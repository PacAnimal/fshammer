#!/bin/sh
# NFSv4: lockf(F_LOCK) (blocking) and unlock in a loop, first from one process, then from four processes
# taking turns on one file. Reports how many lock calls fail; on NFS-Ganesha, with four processes, every lock
# call after the first grant fails with EIO, and with one process none does.
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
import collections, errno, fcntl, json, os, sys
base = sys.argv[1]

def loop(path, label, procs, rounds=16):
    with open(path, "wb") as fh:
        fh.write(b"0".rjust(16))
    for p in range(procs):
        if os.fork() == 0:
            errors = collections.Counter()
            fd = os.open(path, os.O_RDWR)
            for _ in range(rounds):
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX, 16, 0)
                except OSError as e:
                    errors[errno.errorcode.get(e.errno, str(e.errno))] += 1
                    continue
                os.pwrite(fd, str(int(os.pread(fd, 16, 0)) + 1).encode().rjust(16), 0)
                fcntl.lockf(fd, fcntl.LOCK_UN, 16, 0)
            os.close(fd)
            with open(f"{path}.errors-{p}", "w") as out:
                json.dump(errors, out)
            os._exit(0)
    for _ in range(procs):
        os.wait()
    errors = collections.Counter()
    for p in range(procs):
        with open(f"{path}.errors-{p}") as fh:
            errors.update(json.load(fh))
    with open(path, "rb") as fh:
        count = int(fh.read())
    by_errno = ", ".join(f"{n} {name}" for name, n in sorted(errors.items())) or "none"
    print(f"{label}: {procs * rounds} lock calls, failed: {by_errno}, counter reads {count}")
    return sum(errors.values())

alone = loop(base + "-alone", "one process", 1)
together = loop(base + "-together", "four processes", 4)
if alone or together:
    print("REPRODUCED: blocking lock calls failed")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
