#!/bin/sh
# Linux 9p client, cache=mmap: one process appends while another stats the file; appended bytes go missing.
# Usage: 9p-stat-during-append.sh <u9fs|diod>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" mmap 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/observed-$$.log" <<'PY'
import os, sys
path = sys.argv[1]
writes, size = 400, 32768
open(path, "wb").close()
if os.fork() == 0:
    for _ in range(20000):
        os.stat(path)
    os._exit(0)
fd = os.open(path, os.O_WRONLY | os.O_APPEND)
for i in range(writes):
    assert os.write(fd, bytes([65 + i % 26]) * size) == size
os.close(fd)
os.wait()
want, got = writes * size, os.stat(path).st_size
print(f"{want} bytes acknowledged, file is {got}")
if got != want:
    print(f"REPRODUCED: {want - got} acknowledged bytes are missing")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
