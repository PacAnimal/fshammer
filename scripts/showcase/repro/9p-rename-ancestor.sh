#!/bin/sh
# u9fs: after a directory above an open file is renamed, fsync() on the file's descriptor fails with ENOENT.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" u9fs none 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/tall-$$" <<'PY'
import os, sys
top = sys.argv[1]
os.makedirs(os.path.join(top, "a", "b"))
fd = os.open(os.path.join(top, "a", "b", "f.bin"), os.O_WRONLY | os.O_CREAT, 0o644)
os.write(fd, b"before")
os.rename(top, top + "-moved")
try:
    os.write(fd, b" after")
    os.fsync(fd)
except OSError as e:
    print(f"REPRODUCED: the open file's descriptor failed after its ancestor was renamed: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
