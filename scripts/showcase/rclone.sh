#!/bin/sh
# rclone mount of a local directory. Usage: rclone.sh writes|full. Prints the mount point.
set -eu

mode="$1"
base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
"$(dirname "$0")/rclone-install.sh"

mkdir -p "$base/rclone-$mode-remote" "$base/rclone-$mode"
rclone mount "$base/rclone-$mode-remote" "$base/rclone-$mode" --vfs-cache-mode "$mode" \
    --cache-dir "$base/rclone-$mode-cache" --config /dev/null --daemon --daemon-wait 30s >&2
echo "$base/rclone-$mode"
