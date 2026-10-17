#!/bin/sh
# Installs a pinned rclone release, with FUSE, checked against the SHA-256 rclone publishes for it.
set -eu

version=v1.75.2
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fuse3 curl >/dev/null
if [ "$(rclone version 2>/dev/null | head -1)" != "rclone $version" ]; then
    arch="$(dpkg --print-architecture)"
    case "$arch" in
        amd64) sha=efbfe852181f7191eb3c9043ed1ab49c9b2d0ba045c61c926a5da111c939ec5c ;;
        arm64) sha=b8ba161d5c837206fad79ff1aabf0a81aa02a5fb164cdbc0ff5db4160aecd278 ;;
        *)     echo "no pinned checksum for $arch" >&2; exit 1 ;;
    esac
    deb="$(mktemp -d)/rclone.deb"
    curl -fsSL "https://downloads.rclone.org/$version/rclone-$version-linux-$arch.deb" -o "$deb"
    echo "$sha  $deb" | sha256sum -c --quiet -
    sudo dpkg -i "$deb" >/dev/null
fi
