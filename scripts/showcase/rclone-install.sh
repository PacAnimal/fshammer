#!/bin/sh
# Installs a pinned rclone release, with FUSE.
set -eu

version=v1.75.2
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fuse3 curl >/dev/null
if [ "$(rclone version 2>/dev/null | head -1)" != "rclone $version" ]; then
    deb="$(mktemp -d)/rclone.deb"
    curl -fsSL "https://downloads.rclone.org/$version/rclone-$version-linux-$(dpkg --print-architecture).deb" -o "$deb"
    sudo dpkg -i "$deb" >/dev/null
fi
