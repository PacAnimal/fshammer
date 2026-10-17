#!/bin/sh
# bindfs: after link(a, b), data appended through a is not seen when b is read straight after.
# Usage: bindfs-hardlink.sh [seconds to wait before reading b] [bindfs options...]
set -eu
wait="${1:-0}"
[ $# -gt 0 ] && shift
dir="$("$(dirname "$0")/../bindfs.sh" "$@" | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/link-$$" "$wait" <<'PY'
import os, sys, time

def contents(path):
    # what fshammer does before every verification read: ask the kernel to drop the file's cached pages
    with open(path, "rb") as fh:
        os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        return fh.read()

base, wait = sys.argv[1], float(sys.argv[2])
first, second = base + "-a.bin", base + "-b.bin"
body, extra = os.urandom(8192), os.urandom(1024)
with open(first, "wb") as fh:
    fh.write(body)
os.link(first, second)
assert contents(second) == body
print("inodes:", os.stat(first).st_ino, os.stat(second).st_ino, "nlink:", os.stat(first).st_nlink)
with open(first, "ab") as fh:
    fh.write(extra)
time.sleep(wait)
got = contents(second)
print(f"appended {len(extra)} bytes through the first name, waited {wait}s; the second name reads {len(got)} "
      f"of {len(body + extra)}")
if got != body + extra:
    print("REPRODUCED: the second name returned stale content")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
