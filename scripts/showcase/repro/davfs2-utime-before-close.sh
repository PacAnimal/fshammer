#!/bin/sh
# davfs2: a file written, given an mtime with utime() and then closed reads back empty straight after close.
set -eu
dir="$("$(dirname "$0")/../davfs2.sh" | tail -n 1)"
python3 - "$dir/utime-$$.bin" <<'PY'
import os, sys, time
path = sys.argv[1]
data = os.urandom(4096)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.write(fd, data)
when = int(time.time()) - 86400
os.utime(path, (when, when))
os.close(fd)
with open(path, "rb") as fh:
    got = fh.read()
print(f"wrote {len(data)} bytes, read back {len(got)}")
if got != data:
    print("REPRODUCED: the content read back after close differs from what was written")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
