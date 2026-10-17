#!/bin/sh
# NFSv4: four processes on one mount take turns at an exclusive lock on one file (lockf(F_LOCK), blocking),
# increment a counter in it and unlock. Some lock calls fail with EIO.
# Usage: nfs-lock-contention.sh <knfsd|ganesha> <version>
set -eu
here="$(dirname "$0")"
case "$1" in
    knfsd)   setup="$("$here/../knfsd.sh" "$2")" ;;
    ganesha) setup="$("$here/../ganesha.sh" nfs "$2")" ;;
    *)       echo "unknown server: $1" >&2; exit 2 ;;
esac
dir="$(printf '%s\n' "$setup" | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/counter-$$" <<'PY'
import fcntl, os, sys
path, procs, rounds = sys.argv[1], 4, 64
with open(path, "wb") as fh:
    fh.write(b"0".rjust(16))
for p in range(procs):
    if os.fork() == 0:
        errors = 0
        fd = os.open(path, os.O_RDWR)
        for _ in range(rounds):
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX, 16, 0)
            except OSError as e:
                errors += 1
                print(f"process {p}: lockf failed: {e}", flush=True)
                continue
            value = int(os.pread(fd, 16, 0))
            os.pwrite(fd, str(value + 1).encode().rjust(16), 0)
            fcntl.lockf(fd, fcntl.LOCK_UN, 16, 0)
        os.close(fd)
        os._exit(min(errors, 100))
failed = sum(os.waitstatus_to_exitcode(os.wait()[1]) for _ in range(procs))
with open(path, "rb") as fh:
    count = int(fh.read())
print(f"{procs * rounds} locked increments attempted, {failed} lock calls failed, counter reads {count}")
if failed or count != procs * rounds - failed:
    print("REPRODUCED: a blocking exclusive lock failed, or the counter does not match the increments made")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
