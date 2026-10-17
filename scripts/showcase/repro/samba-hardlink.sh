#!/bin/sh
# Samba + Linux SMB3 client: after link(a, b), opening b fails with EINVAL.
set -eu
read -r a _ <<EOT
$("$(dirname "$0")/../samba.sh" | tail -n 1)
EOT
python3 - "$a/link-$$" <<'PY'
import os, sys
base = sys.argv[1]
first, second = base + "-a.bin", base + "-b.bin"
with open(first, "wb") as fh:
    fh.write(b"hello")
os.link(first, second)
print("nlink:", os.stat(first).st_nlink, os.stat(second).st_nlink)
try:
    with open(second, "rb") as fh:
        print("read through the second name:", fh.read())
except OSError as e:
    print(f"REPRODUCED: open of the second name failed: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
