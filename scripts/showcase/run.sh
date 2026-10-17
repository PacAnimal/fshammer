#!/bin/bash
# Mounts one showcase target and runs fshammer against it, requiring the outcome in expected/<target>.txt.
# Usage: run.sh <target>. Linux with sudo, in a throwaway VM or container: see showcase/README.md.
# SHOWCASE_MODE=record runs three times and writes a proposed expectations file instead of asserting.
set -euo pipefail

target="$1"
here="$(cd "$(dirname "$0")" && pwd)"
base="${SHOWCASE_BASE:-/tmp/showcase}"
log="${SHOWCASE_LOG:-$base/log/$target}"
export SHOWCASE_BASE="$base"

args=(--duration 0 --procs 4 --seed 1)
case "$target" in
    tmpfs)           pkgs=();                          mounts="$("$here/tmpfs.sh")" ;;
    ext4)            pkgs=(e2fsprogs);                 mounts="$("$here/localfs.sh" ext4)" ;;
    xfs)             pkgs=(xfsprogs);                  mounts="$("$here/localfs.sh" xfs)" ;;
    btrfs)           pkgs=(btrfs-progs);               mounts="$("$here/localfs.sh" btrfs)" ;;
    overlayfs)       pkgs=();                          mounts="$("$here/overlayfs.sh")" ;;
    davfs2)          pkgs=(davfs2 apache2);            mounts="$("$here/davfs2.sh")" ;;
    sshfs)           pkgs=(sshfs openssh-server);      mounts="$("$here/sshfs.sh")" ;;
    bindfs)          pkgs=(bindfs fuse3);              mounts="$("$here/bindfs.sh")" ;;
    bindfs-single)   pkgs=(bindfs fuse3);              mounts="$("$here/bindfs.sh" | cut -d' ' -f1)" ;;
    rclone)          pkgs=();                          mounts="$("$here/rclone.sh" writes)" ;;
    samba)           pkgs=(samba cifs-utils);          mounts="$("$here/samba.sh")" ;;
    knfsd-v3)        pkgs=(nfs-kernel-server);         mounts="$("$here/knfsd.sh" 3)" ;;
    knfsd-v4.2)      pkgs=(nfs-kernel-server);         mounts="$("$here/knfsd.sh" 4.2)" ;;
    ganesha-v4.1)    pkgs=(nfs-ganesha);               mounts="$("$here/ganesha.sh" 4.1)" ;;
    u9fs-none)       pkgs=();                          mounts="$("$here/v9fs.sh" u9fs none 1)" ;;
    u9fs-mmap)       pkgs=();                          mounts="$("$here/v9fs.sh" u9fs mmap 1)" ;;
    diod-none)       pkgs=(diod);                      mounts="$("$here/v9fs.sh" diod none 1)" ;;
    diod-loose)      pkgs=(diod);                      mounts="$("$here/v9fs.sh" diod loose 1)" ;;
    diod-mmap)       pkgs=(diod);                      mounts="$("$here/v9fs.sh" diod mmap 1)" ;;
    *)               echo "unknown target: $target" >&2; exit 2 ;;
esac

echo "kernel: $(uname -r)"
if [ ${#pkgs[@]} -gt 0 ]; then dpkg-query -W -f '${Package} ${Version}\n' "${pkgs[@]}"; fi
if command -v rclone >/dev/null; then rclone version | head -1; fi

# a fresh directory under each mount; the second mount, when there is one, is --alt-dir, and a third field
# is the server's own directory, for --server-root. "-" stands for an absent second mount.
read -r first second root <<<"$(tail -n 1 <<<"$mounts")"
run="run-$(date +%s)"
args+=(--dir "$first/$run")
checks=(--mounted "$first")
if [ -n "${second:-}" ] && [ "$second" != - ]; then
    args+=(--alt-dir "$second/$run")
    checks+=(--mounted "$second")
fi
if [ -n "${root:-}" ]; then
    args+=(--server-root "$root/$run")
fi

mode=()
[ "${SHOWCASE_MODE:-assert}" = record ] && mode=(--record 3)
python3 "$here/expect.py" --expected "$here/expected/$target.txt" --log "$log" "${mode[@]}" "${checks[@]}" \
    -- "${args[@]}"
