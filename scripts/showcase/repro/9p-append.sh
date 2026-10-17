#!/bin/sh
# Linux 9p client: four processes append to one file with O_APPEND; bytes every write() reported are missing.
# Usage: 9p-append.sh <u9fs|diod> <none|loose|mmap>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" "$2" 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/append-$$.bin" <<'PY'
import os, sys
path = sys.argv[1]
procs, writes, size = 4, 200, 4096
open(path, "wb").close()
for p in range(procs):
    if os.fork() == 0:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        for _ in range(writes):
            assert os.write(fd, bytes([65 + p]) * size) == size
        os.close(fd)
        os._exit(0)
for _ in range(procs):
    os.wait()
want, got = procs * writes * size, os.stat(path).st_size
print(f"{want} bytes acknowledged, file is {got}")
if got != want:
    print(f"REPRODUCED: {want - got} acknowledged bytes are missing")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
