#!/bin/sh
# Builds the reference filesystems the self-tests run against (Linux, needs sudo):
#   an 8 GiB sparse ext4, the same filesystem bind-mounted a second time, and a 768 MiB ext4 small enough to fill.
# All three are handed to the invoking user, because the tests must run unprivileged.
#
#   eval "$(scripts/reference-fs.sh up)"     # mount, and export FSHAMMER_* for this shell
#   python3 -m pytest
#   scripts/reference-fs.sh down             # unmount and delete the images
set -eu

base="${FSHAMMER_REFERENCE_BASE:-${TMPDIR:-/tmp}/fshammer-reference}"
owner="$(id -u):$(id -g)"

up() {
    mkdir -p "$base/ref" "$base/alt" "$base/small"
    # sparse: the images cost what is written, not their size
    truncate -s 8G "$base/ref.img"
    truncate -s 768M "$base/small.img"
    mkfs.ext4 -q -F "$base/ref.img" >&2
    mkfs.ext4 -q -F "$base/small.img" >&2
    sudo mount -o loop "$base/ref.img" "$base/ref"
    sudo mount --bind "$base/ref" "$base/alt"
    sudo mount -o loop "$base/small.img" "$base/small"
    sudo chown "$owner" "$base/ref" "$base/small"
    echo "export FSHAMMER_REFERENCE_DIR='$base/ref'"
    echo "export FSHAMMER_REFERENCE_ALT_DIR='$base/alt'"
    echo "export FSHAMMER_SMALL_DIR='$base/small'"
}

down() {
    # the bind first: it sits on the loop mount and keeps it busy
    for mount in "$base/alt" "$base/ref" "$base/small"; do
        if mountpoint -q "$mount"; then sudo umount "$mount"; fi
    done
    rm -rf "$base"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
