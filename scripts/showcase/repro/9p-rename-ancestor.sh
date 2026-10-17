#!/bin/sh
# u9fs: a file is held open twelve directories down while the top directory is renamed; writing on through the
# held descriptor and fsync() fail.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" u9fs none 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/tall-$$" <<'PY'
import os, sys
top = sys.argv[1]
leaf_dir = os.path.join(top, *[f"lvl{i}" for i in range(12)])
os.makedirs(leaf_dir)
fd = os.open(os.path.join(leaf_dir, "held.bin"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.write(fd, b"a" * 2048)
try:
    os.rename(top, top + "-moved")
except OSError as e:
    print(f"rename failed: errno {e.errno} ({e.strerror})")
    sys.exit(1)
try:
    os.write(fd, b"b" * 2048)
    os.fsync(fd)
except OSError as e:
    print(f"REPRODUCED: the held descriptor failed after the rename above it: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
