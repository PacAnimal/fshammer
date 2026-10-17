#!/bin/sh
# u9fs: renaming an entry directly under the directory it serves fails with the error string "whoops: can't
# happen in u9fs"; the same rename one level down works. The Linux 9p client has no errno for that string and
# returns ESERVERFAULT (526), which is defined only inside the kernel.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" u9fs none 1 | tail -n 1 | cut -d' ' -f1)"
status=0
python3 - "$dir" <<'PY' || status=$?
import os, sys
root = sys.argv[1]
nested = os.path.join(root, f"nested-{os.getpid()}")
os.mkdir(nested)
results = {}
for where, parent in (("directly under the root", root), ("one level down", nested)):
    name = os.path.join(parent, f"d-{os.getpid()}")
    os.mkdir(name)
    try:
        os.rename(name, name + "-moved")
        results[where] = None
        print(f"{where}: rename succeeded")
    except OSError as e:
        results[where] = e.errno
        print(f"{where}: rename failed with errno {e.errno} ({e.strerror})")
if results["directly under the root"] is not None and results["one level down"] is None:
    print("REPRODUCED: a rename directly under the served root fails; one level down it works")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
sudo dmesg | grep 'unknown error' | tail -n 2 || true
exit $status
