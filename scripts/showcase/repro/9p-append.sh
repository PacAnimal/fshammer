#!/bin/sh
# Linux 9p client: four processes append 4096-byte blocks to one file with O_APPEND. Every write() reports the
# full length and each process fsyncs before closing; the check is that the server's own copy of the file
# holds all of them, each block whole.
# Usage: 9p-append.sh <u9fs|diod> <none|loose|mmap>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" "$2" 1 | tail -n 1 | cut -d' ' -f1)"
export="${SHOWCASE_BASE:-/tmp/showcase}/v9fs-export"
python3 - "$dir" "$export" "append-$$.bin" <<'PY'
import os, sys
mount, export, name = sys.argv[1:]
path = os.path.join(mount, name)
procs, writes, size = 4, 200, 4096
open(path, "wb").close()
for p in range(procs):
    if os.fork() == 0:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        for _ in range(writes):
            assert os.write(fd, bytes([65 + p]) * size) == size
        os.fsync(fd)
        os.close(fd)
        os._exit(0)
for _ in range(procs):
    os.wait()
with open(os.path.join(export, name), "rb") as fh:
    data = fh.read()
want = procs * writes * size
# a block is whole if it sits at a multiple of 4096 and holds one writer's letter throughout
whole = sum(1 for at in range(0, len(data) - size + 1, size) if data[at:at + size] == data[at:at + 1] * size)
print(f"{want} bytes acknowledged and fsynced; the server's file is {len(data)}, {whole} of {procs * writes} blocks whole")
if len(data) != want or whole != procs * writes:
    print(f"REPRODUCED: {want - len(data)} bytes missing, {procs * writes - whole} blocks not whole")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
