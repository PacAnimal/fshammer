#!/bin/sh
# Linux 9p client, cache=mmap: one process appends while another stats the file; appended bytes go missing.
# The appender fsyncs before closing, and the check reads the server's own copy of the file.
# Usage: 9p-stat-during-append.sh <u9fs|diod>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" mmap 1 | tail -n 1 | cut -d' ' -f1)"
export="${SHOWCASE_BASE:-/tmp/showcase}/v9fs-export"
python3 - "$dir" "$export" "observed-$$.log" <<'PY'
import os, sys
mount, export, name = sys.argv[1:]
path = os.path.join(mount, name)
writes, size = 400, 32768
open(path, "wb").close()
if os.fork() == 0:
    for _ in range(20000):
        os.stat(path)
    os._exit(0)
fd = os.open(path, os.O_WRONLY | os.O_APPEND)
for i in range(writes):
    assert os.write(fd, bytes([65 + i % 26]) * size) == size
os.fsync(fd)
os.close(fd)
os.wait()
with open(os.path.join(export, name), "rb") as fh:
    data = fh.read()
want = writes * size
# block i holds letter i % 26 throughout when every append landed in order
whole = sum(1 for i in range(writes) if data[i * size:(i + 1) * size] == bytes([65 + i % 26]) * size)
print(f"{want} bytes acknowledged and fsynced; the server's file is {len(data)}, {whole} of {writes} blocks in place")
if len(data) != want or whole != writes:
    print(f"REPRODUCED: {want - len(data)} bytes missing, {writes - whole} blocks not in place")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
