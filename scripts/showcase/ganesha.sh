#!/bin/sh
# NFS-Ganesha with the VFS FSAL exporting a local ext4 directory over NFS (its latest release) or 9P2000.L over
# TCP (V15.8, the last release with a 9P server).
# Usage: ganesha.sh nfs <version, e.g. 3, 4.0, 4.2> [same|2client]   two NFS mounts; see knfsd.sh for same/2client
#        ganesha.sh 9p <cache mode: none|loose|mmap>                one 9P mount with the Linux 9p client
# Prints the mount point(s), "-" for an absent second mount, and the exported directory.
# GANESHA_DEBUG=1 logs everything Ganesha does, to $SHOWCASE_BASE/ganesha.log.
set -eu

proto="$1"
mode="$2"
clients="${3:-same}"
here="$(dirname "$0")"
base="${SHOWCASE_BASE:-/tmp/showcase}"
if [ "$proto" = 9p ]; then "$here/build.sh" ganesha-9p; protocols="9P"; else "$here/build.sh" ganesha; protocols="3, 4"; fi
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common rpcbind >/dev/null

export="$base/ganesha-export"
a="$base/ganesha-$proto-$mode-$clients-a"
b="$base/ganesha-$proto-$mode-$clients-b"
mkdir -p "$a" "$b"
"$here/ext4.sh" "$export"
[ "$clients" = 2client ] && "$here/netns.sh" up

# NFSv3 locking is NLM, and the kernel's own lockd (which the client side starts) registers NLM with rpcbind
# too; on one rpcbind the two collide. So for NFSv3 Ganesha runs in the second network namespace, with its own
# rpcbind and statd, and the client reaches it at 10.231.0.2.
if [ "$proto" = nfs ] && [ "$mode" = 3 ]; then
    "$here/netns.sh" up
    server=10.231.0.2
else
    server=127.0.0.1
fi

sudo mkdir -p /etc/ganesha /var/lib/nfs/ganesha
sudo tee /etc/ganesha/showcase.conf >/dev/null <<CONF
NFS_CORE_PARAM { Protocols = $protocols; Plugins_Dir = /usr/local/lib/ganesha; }
# numeric owners, as knfsd sends by default, so ownership does not depend on an idmapper
NFSV4 { Grace_Period = 10; Lease_Lifetime = 10; Allow_Numeric_Owners = true; Only_Numeric_Owners = true;
        RecoveryRoot = /var/lib/nfs/ganesha; }
$([ "$proto" = 9p ] && echo '_9P { _9P_TCP_Port = 564; }')
$([ "${GANESHA_DEBUG:-}" = 1 ] && echo 'LOG { COMPONENTS { ALL = FULL_DEBUG; } }')
EXPORT {
    Export_Id = 1;
    Path = $export;
    Pseudo = /export;
    Access_Type = RW;
    Squash = No_Root_Squash;
    Protocols = $protocols;
    FSAL { Name = VFS; }
}
CONF
start="mkdir -p /run/ganesha && /usr/local/bin/ganesha.nfsd -f /etc/ganesha/showcase.conf -L $base/ganesha.log \
    -N NIV_EVENT -p /run/ganesha/ganesha.pid"
if [ "$server" = 10.231.0.2 ]; then
    printf '%s\n' "rpcbind -w && rpc.statd && $start" > "$base/ganesha-serve.sh"
    "$here/netns.sh" serve "$base/ganesha-serve.sh"
    probe="sudo ip netns exec fshammer-client2 ss -ltn"
fi
# the host's own: asked of its loopback, since a pgrep would also see the namespace's
rpcinfo -p 127.0.0.1 >/dev/null 2>&1 || sudo rpcbind
# NFSv3 locking (NLM) needs the status monitor on the client too
rpcinfo -p 127.0.0.1 | grep -qw status || sudo rpc.statd
if [ "$server" != 10.231.0.2 ]; then
    sudo sh -c "$start"
    probe="ss -ltn"
fi
port=2049; [ "$proto" = 9p ] && port=564
for _ in $(seq 100); do $probe | grep -q ":$port " && break; sleep 0.2; done

second=-
case "$proto" in
    nfs)
        case "$mode" in 4*) path=/export ;; *) path="$export" ;; esac
        sudo mount -t nfs -o "vers=$mode" "$server:$path" "$a" >&2
        if [ "$clients" = 2client ]; then
            "$here/netns.sh" mount -t nfs -o "vers=$mode" "10.231.0.1:$path" "$b" >&2
        else
            sudo mount -t nfs -o "vers=$mode,nosharecache" "$server:$path" "$b" >&2
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
