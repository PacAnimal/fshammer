#!/bin/sh
# A local filesystem on a sparse 8 GiB loop image, bind-mounted a second time. Usage: localfs.sh ext4|xfs|btrfs.
# Prints the two mount points.
set -eu

fs="$1"
base="${SHOWCASE_BASE:-/tmp/showcase}"
case "$fs" in
    xfs)   pkg=xfsprogs ;;
    btrfs) pkg=btrfs-progs ;;
    *)     pkg=e2fsprogs ;;
esac
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" >/dev/null

a="$base/$fs-a"
b="$base/$fs-b"
mkdir -p "$a" "$b"
truncate -s 8G "$base/$fs.img"
case "$fs" in
    ext4) mkfs.ext4 -q -F "$base/$fs.img" >&2 ;;
    *)    "mkfs.$fs" -q -f "$base/$fs.img" >&2 ;;
esac
sudo mount -o loop "$base/$fs.img" "$a"
sudo chown "$(id -u):$(id -g)" "$a"
sudo mount --bind "$a" "$b"
echo "$a $b"
