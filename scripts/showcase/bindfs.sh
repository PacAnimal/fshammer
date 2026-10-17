#!/bin/sh
# bindfs (FUSE passthrough) over a local directory, mounted twice. Prints the two mount points.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bindfs fuse3 >/dev/null
mkdir -p "$base/bindfs-src" "$base/bindfs-a" "$base/bindfs-b"
bindfs --no-allow-other "$base/bindfs-src" "$base/bindfs-a"
bindfs --no-allow-other "$base/bindfs-src" "$base/bindfs-b"
echo "$base/bindfs-a $base/bindfs-b"
