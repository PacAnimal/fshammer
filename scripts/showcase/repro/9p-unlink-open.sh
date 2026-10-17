#!/bin/sh
# Linux 9p client (u9fs and diod): fstat() on an open file that has been unlinked fails with ENOENT.
# Usage: 9p-unlink-open.sh <u9fs|diod|ganesha>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" none 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/unlinked-$$.bin" <<'PY'
import os, sys
path = sys.argv[1]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
os.write(fd, b"still open")
os.unlink(path)
try:
    print("fstat after unlink: size", os.fstat(fd).st_size)
except OSError as e:
    print(f"REPRODUCED: fstat on the open, unlinked file failed: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
