#!/bin/sh
# Ubuntu 24.04's davfs2 1.7.0 on kernel 6.16 or later: listing a directory fails with EINVAL. Fixed upstream in
# davfs2 1.7.2 ("Increase buf read size"); setting buf_size 64 in davfs2.conf works around it in 1.7.0.
set -eu
dir="$("$(dirname "$0")/../davfs2.sh" ubuntu | tail -n 1)/readdir-$$"
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
