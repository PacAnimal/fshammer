#!/bin/sh
# davfs2: listing a directory fails with EINVAL.
set -eu
dir="$("$(dirname "$0")/../davfs2.sh" | tail -n 1)/readdir-$$"
python3 - "$dir" <<'PY'
import os, sys
d = sys.argv[1]
os.mkdir(d)
open(os.path.join(d, "f"), "w").close()
try:
    print("listdir:", os.listdir(d))
except OSError as e:
    print(f"REPRODUCED: listdir({d!r}) failed: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
