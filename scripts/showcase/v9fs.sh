#!/bin/sh
# The Linux 9p client (v9fs) over TCP to a local 9P server exporting an ext4 directory.
# Usage: v9fs.sh <diod|diod-ubuntu|u9fs|ganesha> <cache mode: none|loose|mmap> <mounts: 1|2; ganesha: 1>
# diod (built from its latest release) speaks 9P2000.L; u9fs (Plan 9's Unix 9P server, built from the
# last commit of its archive) speaks 9P2000.
# Prints the mount point(s), with "-" for an absent second mount, and with cache=none the exported directory.
set -eu

server="$1"
cache="$2"
mounts="$3"
# NFS-Ganesha's 9P server is set up by ganesha.sh, which prints the same fields
if [ "$server" = ganesha ]; then exec "$(dirname "$0")/ganesha.sh" 9p "$cache"; fi
base="${SHOWCASE_BASE:-/tmp/showcase}"
export="$base/v9fs-export"
a="$base/v9fs-$server-$cache-a"
b="$base/v9fs-$server-$cache-b"
mkdir -p "$a" "$b"
"$(dirname "$0")/ext4.sh" "$export"
sudo apt-get update -qq

case "$server" in
    diod)
        "$(dirname "$0")/build.sh" diod
        port=564
        # diod 1.1 stays in the foreground
        sudo /usr/local/sbin/diod --no-auth --export "$export" --listen "127.0.0.1:$port" --logdest stderr \
            >"$base/diod.log" 2>&1 &
        for _ in $(seq 50); do ss -ltn | grep -q ":$port " && break; sleep 0.2; done
        version=9p2000.L
        ;;
    diod-ubuntu)
        # Ubuntu 24.04's package, diod 1.0.24, for comparison with the current release
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq diod >/dev/null
        port=565
        sudo /usr/sbin/diod --no-auth --export "$export" --listen "127.0.0.1:$port" >&2
        version=9p2000.L
        ;;
    u9fs)
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential git socat >/dev/null
        src="$base/u9fs-src"
        if [ ! -x "$src/u9fs" ]; then
            git clone -q https://github.com/Plan9-Archive/u9fs "$src"
            git -C "$src" checkout -q fcec3e1e2e8e8c28b89270e6c44554c4ca9e50a9
            make -s -C "$src" >&2
        fi
        port=5640
        # one u9fs per connection, as inetd would run it; as root, it switches to the attaching user.
        # u9fs serves its fsroot argument, not the attach name, and needs -m for an msize above 8216
        sudo socat "TCP-LISTEN:$port,bind=127.0.0.1,reuseaddr,fork" "EXEC:$src/u9fs -a none -m 1048576 $export" >/dev/null 2>&1 &
        sleep 1
        version=9p2000
        ;;
    *)
        echo "unknown server: $server" >&2
        exit 2
        ;;
esac

# msize 1 MiB on both servers, so the two differ in protocol and not in message size
# attach as the invoking user; 9P2000 carries user names, not ids, so dfltuid/dfltgid map ownership back
opts="trans=tcp,port=$port,aname=$export,version=$version,uname=$(id -un),access=user,msize=1048576,cache=$cache"
opts="$opts,dfltuid=$(id -u),dfltgid=$(id -g)"
sudo mount -t 9p -o "$opts" 127.0.0.1 "$a" >&2
second=-
if [ "$mounts" = 2 ]; then
    sudo mount -t 9p -o "$opts" 127.0.0.1 "$b" >&2
    second="$b"
fi
# with a page cache the client acknowledges writes before the server has them, so the server's directory
# is only offered as --server-root without one
root=
[ "$cache" = none ] && root="$export"
echo "$a $second $root"
