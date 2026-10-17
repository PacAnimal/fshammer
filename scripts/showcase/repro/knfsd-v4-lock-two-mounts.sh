#!/bin/sh
# knfsd over NFSv4.2, the same export mounted twice on one client (the second with nosharecache): a lock held
# through one mount does not exclude the same lock through the other.
set -eu
here="$(dirname "$0")"
read -r a b _ <<EOT
$("$here/../knfsd.sh" 4.2 | tail -n 1)
EOT
python3 "$here/lock-two-mounts.py" "$a/lock-$$.bin" "$b/lock-$$.bin"
