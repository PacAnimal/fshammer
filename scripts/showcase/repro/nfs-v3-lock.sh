#!/bin/sh
# NFSv3: one process takes an fcntl lock on a file nobody else has open; the call does not return.
# Usage: nfs-v3-lock.sh <knfsd|ganesha>
set -eu
here="$(dirname "$0")"
case "$1" in
    knfsd)   setup="$("$here/../knfsd.sh" 3)" ;;
    ganesha) setup="$("$here/../ganesha.sh" nfs 3)" ;;
    *)       echo "unknown server: $1" >&2; exit 2 ;;
esac
dir="$(printf '%s\n' "$setup" | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/v3lock-$$.bin" <<'PY'
import fcntl, os, signal, sys, time
path, patience = sys.argv[1], 120
open(path, "wb").close()
child = os.fork()
if child == 0:
    fd = os.open(path, os.O_RDWR)
    fcntl.lockf(fd, fcntl.LOCK_EX, 0, 0)
    os._exit(0)
deadline = time.monotonic() + patience
while time.monotonic() < deadline:
    pid, status = os.waitpid(child, os.WNOHANG)
    if pid:
        print(f"the lock call returned (exit status {os.waitstatus_to_exitcode(status)})")
        print("not reproduced")
        sys.exit(1)
    time.sleep(1)
os.kill(child, signal.SIGKILL)       # the wait is killable (rpc_wait_bit_killable)
os.waitpid(child, 0)
print(f"REPRODUCED: lockf on an uncontended file did not return within {patience} s")
sys.exit(0)
PY
