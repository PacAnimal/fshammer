#!/bin/sh
# Linux 9p client, 9P2000 (u9fs): a second process is granted an exclusive fcntl lock on a range another
# process holds exclusively.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" u9fs none 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/lock-$$.bin" <<'PY'
import fcntl, os, sys, time
path = sys.argv[1]
open(path, "wb").close()
held, ready = os.pipe(), os.pipe()
if os.fork() == 0:
    fd = os.open(path, os.O_RDWR)
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 0)
    os.write(ready[1], b"x")
    os.read(held[0], 1)
    os._exit(0)
os.read(ready[0], 1)
fd = os.open(path, os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 0)
    granted = True
except OSError as e:
    print("second lock refused:", e)
    granted = False
os.write(held[1], b"x")
os.wait()
if granted:
    print("REPRODUCED: two processes both hold an exclusive lock on bytes 0-63")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
