#!/bin/sh
# bindfs: after link(a, b) and a read through b, data appended through a is not seen when b is read again.
set -eu
dir="$("$(dirname "$0")/../bindfs.sh" | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/link-$$" <<'PY'
import os, sys
base = sys.argv[1]
first, second = base + "-a.bin", base + "-b.bin"
body, extra = b"x" * 8192, b"y" * 1024
with open(first, "wb") as fh:
    fh.write(body)
os.link(first, second)
with open(second, "rb") as fh:
    fh.read()
with open(first, "ab") as fh:
    fh.write(extra)
with open(second, "rb") as fh:
    got = fh.read()
print(f"appended {len(extra)} bytes through the first name; the second name reads {len(got)} of {len(body + extra)}")
if got != body + extra:
    print("REPRODUCED: the second name returned stale content")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
