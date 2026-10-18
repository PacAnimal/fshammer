#!/bin/sh
# NFS-Ganesha's 9P server with the Linux 9p client (9P2000.L): creating a file fails with EFAULT, and the
# directory then cannot be listed (EIO). Prints the negotiated msize, what Ganesha logged for the create, and
# whether root can mkdir.
set -eu
base="${SHOWCASE_BASE:-/tmp/showcase}"
dir="$(GANESHA_DEBUG=1 "$(dirname "$0")/../ganesha.sh" 9p none | tail -n 1 | cut -d' ' -f1)"
echo "mount: $(grep " $dir " /proc/mounts | tr ',' '\n' | grep -E '^msize=|^cache=' | tr '\n' ' ')"
echo "== create, as $(id -un)"
status=0
python3 - "$dir" <<'PY' || status=$?
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
echo "== what Ganesha logged for the create"
grep -E "TLCREATE|Opened fd=.* for file created-|RLERROR|RERROR\(_9P_TLCREATE" "$base/ganesha.log" | sed 's/^.*\] //' | tail -n 6 || true
echo "== mkdir as root"
sudo mkdir "$dir/as-root-$$" && echo "mkdir as root succeeded" || echo "mkdir as root failed"
exit $status
