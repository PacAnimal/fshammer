#!/bin/sh
# NFS-Ganesha, built from its latest release, with the VFS FSAL exporting a local ext4 directory over NFS and
# 9P (9P2000.L over TCP).
# Usage: ganesha.sh nfs <version, e.g. 3, 4.0, 4.2> [same|2client]   two NFS mounts; see knfsd.sh for same/2client
#        ganesha.sh 9p <cache mode: none|loose|mmap>                one 9P mount with the Linux 9p client
# Prints the mount point(s), "-" for an absent second mount, and the exported directory.
set -eu

proto="$1"
mode="$2"
clients="${3:-same}"
here="$(dirname "$0")"
base="${SHOWCASE_BASE:-/tmp/showcase}"
"$here/build.sh" ganesha
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common rpcbind >/dev/null

export="$base/ganesha-export"
a="$base/ganesha-$proto-$mode-$clients-a"
b="$base/ganesha-$proto-$mode-$clients-b"
mkdir -p "$a" "$b"
"$here/ext4.sh" "$export"
[ "$clients" = 2client ] && "$here/netns.sh" up

sudo mkdir -p /etc/ganesha /var/run/ganesha /var/lib/nfs/ganesha
sudo tee /etc/ganesha/showcase.conf >/dev/null <<CONF
NFS_CORE_PARAM { Protocols = 3, 4, 9P; Plugins_Dir = /usr/local/lib/ganesha; }
# numeric owners, as knfsd sends by default, so ownership does not depend on an idmapper
NFSV4 { Grace_Period = 10; Lease_Lifetime = 10; Allow_Numeric_Owners = true; Only_Numeric_Owners = true;
        RecoveryRoot = /var/lib/nfs/ganesha; }
_9P { _9P_TCP_Port = 564; }
EXPORT {
    Export_Id = 1;
    Path = $export;
    Pseudo = /export;
    Access_Type = RW;
    Squash = No_Root_Squash;
    Protocols = 3, 4, 9P;
    FSAL { Name = VFS; }
}
CONF
pgrep -x rpcbind >/dev/null || sudo rpcbind
# NFSv3 locking (NLM) needs the status monitor on the client
pgrep -x rpc.statd >/dev/null || sudo rpc.statd
sudo /usr/local/bin/ganesha.nfsd -f /etc/ganesha/showcase.conf -L "$base/ganesha.log" -N NIV_EVENT \
    -p /var/run/ganesha/ganesha.pid
for _ in $(seq 100); do ss -ltn | grep -q ':2049 ' && ss -ltn | grep -q ':564 ' && break; sleep 0.2; done

second=-
case "$proto" in
    nfs)
        case "$mode" in 4*) path=/export ;; *) path="$export" ;; esac
        sudo mount -t nfs -o "vers=$mode" "127.0.0.1:$path" "$a" >&2
        if [ "$clients" = 2client ]; then
            "$here/netns.sh" mount -t nfs -o "vers=$mode" "10.231.0.1:$path" "$b" >&2
        else
            sudo mount -t nfs -o "vers=$mode,nosharecache" "127.0.0.1:$path" "$b" >&2
        fi
        second="$b"
        ;;
    9p)
        opts="trans=tcp,port=564,aname=$export,version=9p2000.L,uname=$(id -un),access=user,msize=1048576"
        sudo mount -t 9p -o "$opts,cache=$mode,dfltuid=$(id -u),dfltgid=$(id -g)" 127.0.0.1 "$a" >&2
        ;;
    *)
        echo "unknown protocol: $proto" >&2
        exit 2
        ;;
esac
# locks are refused during the grace period after start
sleep 15
root="$export"
# with a page cache the client acknowledges writes before the server has them
[ "$proto" = 9p ] && [ "$mode" != none ] && root=
echo "$a $second $root"
