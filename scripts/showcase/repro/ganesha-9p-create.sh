#!/bin/sh
# NFS-Ganesha's 9P server with the Linux 9p client (9P2000.L): creating a file fails with EFAULT, although the
# server's log shows it opened the new file; the directory then cannot be listed (EIO).
set -eu
dir="$("$(dirname "$0")/../ganesha.sh" 9p none | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir" <<'PY'
import os, sys
d = sys.argv[1]
problems = []
try:
    with open(os.path.join(d, f"created-{os.getpid()}.bin"), "wb") as fh:
        fh.write(b"hello")
    print("create succeeded")
except OSError as e:
    problems.append(f"create failed: {e}")
try:
    print("listing:", len(os.listdir(d)), "entries")
except OSError as e:
    problems.append(f"listing failed: {e}")
if problems:
    print("REPRODUCED: " + "; ".join(problems))
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
