#!/bin/sh
# sshfs: the inode number in a directory listing differs from the one stat() returns for the same file.
# Usage: sshfs-readdir-inode.sh [sshfs -o options, e.g. readdir_ino]
set -eu
read -r a _ <<EOT
$("$(dirname "$0")/../sshfs.sh" "${1:-}" | tail -n 1)
EOT
python3 - "$a/ino-$$" <<'PY'
import os, sys
d = sys.argv[1]
os.mkdir(d)
open(os.path.join(d, "f"), "w").close()
listed = next(os.scandir(d)).inode()
stated = os.stat(os.path.join(d, "f")).st_ino
print(f"readdir inode {listed}, stat inode {stated}")
if listed != stated:
    print("REPRODUCED: readdir and stat disagree on the inode number")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
