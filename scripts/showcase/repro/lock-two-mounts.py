"""Holds an exclusive lock on bytes 0-63 through one path and tries the same lock through another.

Exits 0 (reproduced) if the second lock is granted. Usage: lock-two-mounts.py <path via mount A> <path via mount B>
"""
import fcntl, os, sys

first, second = sys.argv[1], sys.argv[2]
with open(first, "wb") as fh:
    fh.write(b"\0" * 64)
held, ready = os.pipe(), os.pipe()
if os.fork() == 0:
    fd = os.open(first, os.O_RDWR)
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 0)
    os.write(ready[1], b"x")
    os.read(held[0], 1)
    os._exit(0)
os.read(ready[0], 1)
fd = os.open(second, os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 0)
    granted = True
except OSError as e:
    print("lock through the second mount refused:", e)
    granted = False
os.write(held[1], b"x")
os.wait()
if granted:
    print("REPRODUCED: the same range is held exclusively through both mounts at once")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
