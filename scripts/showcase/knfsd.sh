#!/bin/sh
# Linux kernel NFS server (knfsd) exporting a local ext4 directory, mounted twice.
# Usage: knfsd.sh <nfs version, e.g. 3 or 4.2> [same|2client]
#   same     both mounts over loopback; the second uses nosharecache, so it has its own caches but shares the
#            first mount's NFS client (one NFSv4 client ID)
#   2client  the second mount comes from another network namespace (netns.sh), so the server sees two clients
# Prints the two mount points and the exported directory.
set -eu

vers="$1"
clients="${2:-same}"
here="$(dirname "$0")"
base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-kernel-server >/dev/null
# the package starts the server on install where systemd runs; the settings below need it stopped
sudo systemctl stop nfs-server 2>/dev/null || true

export="$base/knfsd-export"
a="$base/knfsd-$vers-$clients-a"
b="$base/knfsd-$vers-$clients-b"
mkdir -p "$export" "$a" "$b"
"$here/ext4.sh" "$export"
[ "$clients" = 2client ] && "$here/netns.sh" up
opts="rw,sync,no_subtree_check,no_root_squash,fsid=0"
echo "$export 127.0.0.1($opts) 10.231.0.0/24($opts)" | sudo tee /etc/exports >/dev/null

# started by hand rather than through systemd, so it also works in a container
mountpoint -q /proc/fs/nfsd || sudo mount -t nfsd nfsd /proc/fs/nfsd
pgrep -x rpcbind >/dev/null || sudo rpcbind
# NFSv3 locking (NLM) needs the status monitor on the client
pgrep -x rpc.statd >/dev/null || sudo rpc.statd
sudo exportfs -ra
pgrep -x rpc.mountd >/dev/null || sudo rpc.mountd
# locks are refused during the server's grace period after start; shorten it, then wait it out
sudo rpc.nfsd 0
echo 10 | sudo tee /proc/fs/nfsd/nfsv4gracetime /proc/fs/nfsd/nfsv4leasetime >/dev/null
sudo sysctl -q fs.nfs.nlm_grace_period=10 2>/dev/null || true
sudo rpc.nfsd 8

# fsid=0 makes the export the NFSv4 pseudo-root, so v4 mounts it as /
case "$vers" in 4*) path=/ ;; *) path="$export" ;; esac
sudo mount -t nfs -o "vers=$vers" "127.0.0.1:$path" "$a" >&2
if [ "$clients" = 2client ]; then
    "$here/netns.sh" mount -t nfs -o "vers=$vers" "10.231.0.1:$path" "$b" >&2
else
    sudo mount -t nfs -o "vers=$vers,nosharecache" "127.0.0.1:$path" "$b" >&2
fi
sleep 15
# NFSv4 clients the server holds state for
[ -d /proc/fs/nfsd/clients ] && echo "knfsd NFSv4 clients: $(sudo ls /proc/fs/nfsd/clients | wc -l)" >&2
echo "$a $b $export"
