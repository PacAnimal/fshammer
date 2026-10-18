#!/bin/bash
# Builds and installs a pinned upstream release into /usr/local, from a tarball checked against its SHA-256.
# Usage: build.sh <libfuse|sshfs|bindfs|davfs2|diod|ganesha|ganesha-9p>. Does nothing if that version is already
# installed. nfs-ganesha publishes no tarball, so it is cloned at its release tag and checked against the tag's commit.
# ganesha is the latest release, for NFS; V15.9 removed the 9P server, so ganesha-9p stays on V15.8.
set -euo pipefail

name="$1"
case "$name" in
    libfuse) version=3.18.3
             url=https://github.com/libfuse/libfuse/releases/download/fuse-$version/fuse-$version.tar.gz
             sha=bcd19582c5e30f7fe45dd86a5540e998590aa01903afc7ebcbeea6c8ac5421ee
             deps=(meson ninja-build pkg-config) ;;
    sshfs)   version=3.7.6
             url=https://github.com/libfuse/sshfs/releases/download/sshfs-$version/sshfs-$version.tar.xz
             sha=6a1bcb31450a077e9cb1b7ff158c71de34db697c3c0da6cb362502131e495893
             deps=(meson ninja-build pkg-config libglib2.0-dev openssh-client) ;;
    bindfs)  version=1.18.4
             url=https://bindfs.org/downloads/bindfs-$version.tar.gz
             sha=3266d0aab787a9328bbb0ed561a371e19f1ff077273e6684ca92a90fedb2fe24
             deps=(build-essential pkg-config) ;;
    davfs2)  version=1.7.3
             url=https://download.savannah.nongnu.org/releases/davfs2/davfs2-$version.tar.gz
             sha=a5368161eb5055651d7e5e8180c1606da95e78c941b6bb8a9286df7923cfcba9
             deps=(build-essential pkg-config libneon27-dev) ;;
    diod)    version=1.1.0
             url=https://github.com/chaos/diod/releases/download/v$version/diod-$version.tar.gz
             sha=93cc05fdc19352d1bf30f716e78d040bf9d6d128cc9cf3579b1d8b36a0898233
             deps=(build-essential pkg-config libncurses-dev libcap-dev) ;;
    ganesha) version=15.9
             url=https://github.com/nfs-ganesha/nfs-ganesha.git
             sha=42bc41b62c12346aea30357cb613944e1032890b
             deps=(build-essential cmake git bison flex pkg-config liburcu-dev libcap-dev libblkid-dev uuid-dev
                   libnfsidmap-dev libacl1-dev libkrb5-dev libnsl-dev libsqlite3-dev libssl-dev) ;;
    ganesha-9p) version=15.8
             url=https://github.com/nfs-ganesha/nfs-ganesha.git
             sha=64912e90ec8d5a654c18cc5d0fb4795a4c28422b
             deps=(build-essential cmake git bison flex pkg-config liburcu-dev libcap-dev libblkid-dev uuid-dev
                   libnfsidmap-dev libacl1-dev libkrb5-dev libnsl-dev libsqlite3-dev libssl-dev) ;;
    *)       echo "unknown component: $name" >&2; exit 2 ;;
esac

marker="/usr/local/share/showcase/$name-$version"
[ -e "$marker" ] && exit 0

# sshfs and bindfs link against the libfuse built here, not the distribution's
case "$name" in sshfs|bindfs) "$0" libfuse ;; esac

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl "${deps[@]}" >/dev/null
work="$(mktemp -d)"
case "$name" in ganesha*) clone=1 ;; *) clone= ;; esac
if [ -n "$clone" ]; then
    git clone -q --depth 1 --branch "V$version" --recurse-submodules --shallow-submodules "$url" "$work/src"
    [ "$(git -C "$work/src" rev-parse HEAD)" = "$sha" ] || { echo "V$version is not $sha" >&2; exit 1; }
else
    curl -fsSL "$url" -o "$work/src.tar"
    echo "$sha  $work/src.tar" | sha256sum -c --quiet -
    tar -xf "$work/src.tar" -C "$work"
fi
src="$(find "$work" -mindepth 1 -maxdepth 1 -type d)"
export PKG_CONFIG_PATH="/usr/local/lib/$(gcc -dumpmachine 2>/dev/null || uname -m)/pkgconfig:/usr/local/lib/pkgconfig"

case "$name" in
    libfuse)
        # its install makes fusermount3 setuid root, which mounting as an ordinary user needs
        meson setup --prefix=/usr/local -Dexamples=false -Dtests=false -Denable-io-uring=false \
            -Dinitscriptdir= "$work/build" "$src" >&2
        ninja -C "$work/build" >&2
        sudo ninja -C "$work/build" install >&2
        ;;
    sshfs)
        meson setup --prefix=/usr/local "$work/build" "$src" >&2
        ninja -C "$work/build" >&2
        sudo ninja -C "$work/build" install >&2
        ;;
    davfs2)
        # the davfs2 user mount.davfs drops to
        getent passwd davfs2 >/dev/null || sudo useradd --system --no-create-home --shell /usr/sbin/nologin davfs2
        (cd "$src" && ./configure -q --prefix=/usr/local --sysconfdir=/etc >&2 && make -s >&2 && sudo make -s install >&2)
        ;;
    diod)
        (cd "$src" && ./configure -q --prefix=/usr/local --disable-auth --disable-config >&2 && make -s >&2 \
            && sudo make -s install >&2)
        ;;
    ganesha|ganesha-9p)
        # the VFS FSAL, the 9P server where the release still has one, every optional integration off
        nine=; [ "$name" = ganesha-9p ] && nine=-DUSE_9P=ON
        cmake -S "$src/src" -B "$work/build" -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
            $nine -DUSE_FSAL_VFS=ON -DUSE_FSAL_CEPH=OFF -DUSE_FSAL_GLUSTER=OFF -DUSE_FSAL_GPFS=OFF \
            -DUSE_FSAL_RGW=OFF -DUSE_FSAL_XFS=OFF -DUSE_FSAL_LUSTRE=OFF -DUSE_FSAL_PROXY_V4=OFF \
            -DUSE_FSAL_PROXY_V3=OFF -DUSE_FSAL_KVSFS=OFF -DUSE_FSAL_LIZARDFS=OFF -DUSE_FSAL_SAUNAFS=OFF \
            -DUSE_FSAL_MEM=OFF -DUSE_GSS=OFF -D_MSPAC_SUPPORT=OFF -DUSE_DBUS=OFF -DUSE_ADMIN_TOOLS=OFF \
            -DUSE_GUI_ADMIN_TOOLS=OFF -DUSE_RADOS_RECOV=OFF -DRADOS_URLS=OFF -DUSE_LTTNG=OFF \
            -DUSE_MONITORING=OFF -DALLOCATOR=libc >&2
        cmake --build "$work/build" -j "$(nproc)" >&2
        sudo cmake --install "$work/build" >&2
        ;;
    *)
        (cd "$src" && ./configure -q --prefix=/usr/local >&2 && make -s >&2 && sudo make -s install >&2)
        ;;
esac
sudo ldconfig
sudo mkdir -p "$(dirname "$marker")"
sudo touch "$marker"
rm -rf "$work"
echo "built $name $version" >&2
