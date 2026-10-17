#!/bin/sh
# overlayfs with an empty lower directory, its upper and work directories on a loop-mounted ext4 image,
# bind-mounted a second time. Prints the two mount points.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
store="$base/overlay-store"
a="$base/overlay-a"
b="$base/overlay-b"
mkdir -p "$a" "$b"
"$(dirname "$0")/ext4.sh" "$store"
mkdir -p "$store/lower" "$store/upper" "$store/work"
sudo mount -t overlay overlay -o "lowerdir=$store/lower,upperdir=$store/upper,workdir=$store/work" "$a"
sudo chown "$(id -u):$(id -g)" "$a"
sudo mount --bind "$a" "$b"
echo "$a $b"
