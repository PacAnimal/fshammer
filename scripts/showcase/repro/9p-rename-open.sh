#!/bin/sh
# Linux 9p client with diod: after a file held open is renamed, fstat() on the descriptor fails with ENOENT,
# and after a new file is created at the old name, writes through the descriptor land in the new file.
# Usage: 9p-rename-open.sh <diod|diod-ubuntu|u9fs> <none|loose|mmap>
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" "$1" "$2" 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/rot-$$" <<'PY'
import os, sys
base = sys.argv[1]
os.mkdir(base)
log, rotated = os.path.join(base, "service.log"), os.path.join(base, "service.log.1")
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.write(fd, b"A" * 100)
os.rename(log, rotated)                       # logrotate moves the file away
problems = []
try:
    size = os.fstat(fd).st_size
    print(f"fstat after rename: {size} bytes")
except OSError as e:
    problems.append(f"fstat after rename failed: {e}")
open(log, "wb").close()                       # and creates a new, empty file at the old name
os.write(fd, b"B" * 100)                      # the daemon keeps writing to the file it has open
os.close(fd)
with open(rotated, "rb") as fh:
    old = len(fh.read())
with open(log, "rb") as fh:
    new = len(fh.read())
print(f"renamed file: {old} bytes (expected 200); new file at the old name: {new} bytes (expected 0)")
if old != 200 or new != 0:
    problems.append("writes through the descriptor did not land in the renamed file")
if problems:
    print("REPRODUCED: " + "; ".join(problems))
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
