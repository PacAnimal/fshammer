#!/bin/sh
# Mounts a fresh sparse 8 GiB ext4 image at <dir>, owned by the invoking user: the backing store for a server.
set -eu

dir="$1"
mkdir -p "$dir"
truncate -s 8G "$dir.img"
mkfs.ext4 -q -F "$dir.img" >&2
sudo mount -o loop "$dir.img" "$dir"
sudo chown "$(id -u):$(id -g)" "$dir"
