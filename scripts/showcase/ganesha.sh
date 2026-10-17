#!/bin/sh
# NFS-Ganesha (VFS FSAL) exporting a local ext4 directory, mounted twice over loopback; the second mount uses
# nosharecache so it is a separate client instance.
# Usage: ganesha.sh <nfs version, e.g. 3 or 4.1>. Prints the two mount points and the exported directory.
set -eu

vers="$1"
base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-ganesha nfs-ganesha-vfs nfs-common >/dev/null

export="$base/ganesha-export"
a="$base/ganesha-$vers-a"
b="$base/ganesha-$vers-b"
mkdir -p "$a" "$b"
"$(dirname "$0")/ext4.sh" "$export"

sudo tee /etc/ganesha/showcase.conf >/dev/null <<CONF
NFS_CORE_PARAM { Protocols = 3, 4; }
# numeric owners, as knfsd sends by default, so ownership does not depend on an idmapper
NFSV4 { Grace_Period = 10; Lease_Lifetime = 10; Allow_Numeric_Owners = true; Only_Numeric_Owners = true; }
EXPORT {
    Export_Id = 1;
    Path = $export;
    Pseudo = /export;
    Access_Type = RW;
    Squash = No_Root_Squash;
    Protocols = 3, 4;
    FSAL { Name = VFS; }
}
CONF
pgrep -x rpcbind >/dev/null || sudo rpcbind
# NFSv3 locking (NLM) needs the status monitor on the client
pgrep -x rpc.statd >/dev/null || sudo rpc.statd
sudo mkdir -p /var/run/ganesha /var/lib/nfs/ganesha
sudo ganesha.nfsd -f /etc/ganesha/showcase.conf -L "$base/ganesha.log" -N NIV_EVENT
for _ in $(seq 100); do ss -ltn | grep -q ':2049 ' && break; sleep 0.2; done

case "$vers" in 4*) path=/export ;; *) path="$export" ;; esac
sudo mount -t nfs -o "vers=$vers" "127.0.0.1:$path" "$a" >&2
sudo mount -t nfs -o "vers=$vers,nosharecache" "127.0.0.1:$path" "$b" >&2
# locks are refused during the grace period after start
sleep 15
echo "$a $b $export"
