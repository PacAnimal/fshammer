#!/bin/sh
# bindfs (FUSE passthrough), built from its latest release against the latest libfuse, over a local directory,
# mounted twice. Extra arguments are passed to bindfs. Prints the two mount points.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
"$(dirname "$0")/build.sh" bindfs
mkdir -p "$base/bindfs-src" "$base/bindfs-a" "$base/bindfs-b"
/usr/local/bin/bindfs --no-allow-other "$@" "$base/bindfs-src" "$base/bindfs-a"
/usr/local/bin/bindfs --no-allow-other "$@" "$base/bindfs-src" "$base/bindfs-b"
echo "$base/bindfs-a $base/bindfs-b"
