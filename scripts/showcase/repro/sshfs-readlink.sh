#!/bin/sh
# sshfs: readlink() on a symlink created through the mount fails with EPERM.
set -eu
read -r a _ <<EOT
$("$(dirname "$0")/../sshfs.sh" | tail -n 1)
EOT
python3 - "$a/link-$$" <<'PY'
import os, sys
link = sys.argv[1]
os.symlink("some/target", link)
print("lstat says symlink:", os.path.islink(link))
try:
    print("readlink:", os.readlink(link))
except OSError as e:
    print(f"REPRODUCED: readlink failed: {e}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
