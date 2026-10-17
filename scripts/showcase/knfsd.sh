#!/bin/sh
# Linux kernel NFS server (knfsd) exporting a local directory, mounted twice over loopback; the second
# mount uses nosharecache so it is a separate client instance.
# Usage: knfsd.sh <nfs version, e.g. 3 or 4.2>. Prints the two mount points and the exported directory.
set -eu

vers="$1"
base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-kernel-server >/dev/null

export="$base/knfsd-export"
a="$base/knfsd-$vers-a"
b="$base/knfsd-$vers-b"
mkdir -p "$export" "$a" "$b"
"$(dirname "$0")/ext4.sh" "$export"
echo "$export 127.0.0.1(rw,sync,no_subtree_check,no_root_squash,fsid=0)" | sudo tee /etc/exports >/dev/null

# started by hand rather than through systemd, so it also works in a container
mountpoint -q /proc/fs/nfsd || sudo mount -t nfsd nfsd /proc/fs/nfsd
pgrep -x rpcbind >/dev/null || sudo rpcbind
# NFSv3 locking (NLM) needs the status monitor on the client
pgrep -x rpc.statd >/dev/null || sudo rpc.statd
sudo exportfs -ra
pgrep -x rpc.mountd >/dev/null || sudo rpc.mountd
# locks are refused during the server's grace period after start; shorten it, then wait it out
echo 10 | sudo tee /proc/fs/nfsd/nfsv4gracetime /proc/fs/nfsd/nfsv4leasetime >/dev/null
sudo sysctl -q fs.nfs.nlm_grace_period=10 2>/dev/null || true
sudo rpc.nfsd 8

# fsid=0 makes the export the NFSv4 pseudo-root, so v4 mounts it as /
case "$vers" in 4*) path=/ ;; *) path="$export" ;; esac
sudo mount -t nfs -o "vers=$vers" "127.0.0.1:$path" "$a" >&2
sudo mount -t nfs -o "vers=$vers,nosharecache" "127.0.0.1:$path" "$b" >&2
sleep 15
echo "$a $b $export"
