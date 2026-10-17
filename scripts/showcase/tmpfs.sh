#!/bin/sh
# tmpfs, mounted once and bind-mounted a second time: the control. Prints the two mount points.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
mkdir -p "$base/tmpfs-a" "$base/tmpfs-b"
sudo mount -t tmpfs -o size=8g,uid="$(id -u)",gid="$(id -g)" tmpfs "$base/tmpfs-a"
sudo mount --bind "$base/tmpfs-a" "$base/tmpfs-b"
echo "$base/tmpfs-a $base/tmpfs-b"
