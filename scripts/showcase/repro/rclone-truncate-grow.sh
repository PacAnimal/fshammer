#!/bin/sh
# rclone mount --vfs-cache-mode writes: extending a file with truncate() fails with EIO.
set -eu
dir="$("$(dirname "$0")/../rclone.sh" writes | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/grow-$$.bin" <<'PY'
import os, sys
path = sys.argv[1]
with open(path, "wb") as fh:
    fh.write(b"x" * 100)
try:
    os.truncate(path, 4096)
except OSError as e:
    print(f"REPRODUCED: truncate to a larger size failed: {e}")
    sys.exit(0)
print("size after truncate:", os.stat(path).st_size)
print("not reproduced")
sys.exit(1)
PY
