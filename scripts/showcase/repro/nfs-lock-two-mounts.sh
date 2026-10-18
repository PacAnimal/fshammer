#!/bin/sh
# NFS, the same export mounted twice: a lock held through one mount does not exclude the same lock through
# the other. Usage: nfs-lock-two-mounts.sh <knfsd|ganesha> <version> <same|2client>
#   same     both mounts on one NFS client (the second with nosharecache)
#   2client  the second mount from another network namespace, so the server sees two clients
set -eu
here="$(dirname "$0")"
case "$1" in
    knfsd)   setup="$("$here/../knfsd.sh" "$2" "$3")" ;;
    ganesha) setup="$("$here/../ganesha.sh" nfs "$2" "$3")" ;;
    *)       echo "unknown server: $1" >&2; exit 2 ;;
esac
read -r a b _ <<EOT
$(printf '%s\n' "$setup" | tail -n 1)
EOT
python3 "$here/lock-two-mounts.py" "$a/lock-$$.bin" "$b/lock-$$.bin"
