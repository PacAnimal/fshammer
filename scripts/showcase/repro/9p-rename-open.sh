#!/bin/sh
# diod 1.1.0: a file held open is renamed and a new file created at the old name, as logrotate does. With
# cache=none, fstat() on the descriptor then fails with ENOENT; with cache=loose, reading the renamed file fails
# with ENODATA. diod 1.0.24 handles both.
# Usage: 9p-rename-open.sh <diod|diod-ubuntu|u9fs|ganesha> <none|loose|mmap>
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
def size(path):
    try:
        with open(path, "rb") as fh:
            return len(fh.read())
    except OSError as e:
        problems.append(f"reading {os.path.basename(path)} failed: {e}")
        return None
old, new = size(rotated), size(log)
print(f"renamed file: {old} bytes (expected 200); new file at the old name: {new} bytes (expected 0)")
if old is not None and new is not None and (old != 200 or new != 0):
    problems.append("writes through the descriptor did not land in the renamed file")
if problems:
    print("REPRODUCED: " + "; ".join(problems))
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
