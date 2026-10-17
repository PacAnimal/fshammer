#!/bin/bash
# Mounts one showcase target and runs fshammer against it, requiring the outcome recorded below.
# Usage: run.sh <target>. Linux with sudo; the workflow runs each target on a fresh runner.
set -euo pipefail

target="$1"
here="$(cd "$(dirname "$0")" && pwd)"
base="${SHOWCASE_BASE:-/tmp/showcase}"
log="${SHOWCASE_LOG:-$base/log/$target}"
export SHOWCASE_BASE="$base"

args=(--duration 0 --procs 4 --seed 1)
expect=()
case "$target" in
    tmpfs)
        mounts="$("$here/tmpfs.sh")"
        code=0
        ;;
    davfs2)
        mounts="$("$here/davfs2.sh")"
        code=1
        expect=(
            "FAIL identity/utimes_survives_the_flush:"
            "FAIL malice/unlink_open:"
            "FAIL namespace/hostile_names:"
            "FAIL namespace/long_names:"
        )
        ;;
    davfs2-declared)
        mounts="$("$here/davfs2.sh")"
        args+=(--max-name-length 248 --declare-gap malice/unlink_open)
        code=1
        expect=(
            "FAIL identity/utimes_survives_the_flush:"
            "FAIL namespace/hostile_names:"
        )
        ;;
    sshfs)
        mounts="$("$here/sshfs.sh")"
        code=1
        expect=(
            "FAIL identity/ctime_is_not_just_mtime:"
            "FAIL identity/sentinel_mtimes:"
            "FAIL inode/readdir_agrees_under_mutation:"
            "FAIL inode/rename_keeps_inode:"
            "FAIL inode/unique_inodes:"
            "FAIL locks/cross_mount_excludes:"
            "FAIL namespace/hardlink:"
            "FAIL namespace/hostile_names:"
            "FAIL walk/walk_manifest:"
            "block frame broken — an append landed where it should not"
        )
        ;;
    bindfs)
        mounts="$("$here/bindfs.sh")"
        code=1
        expect=(
            "FAIL locks/cross_mount_excludes:"
            "block frame broken — an append landed where it should not"
        )
        ;;
    bindfs-single)
        mounts="$("$here/bindfs.sh" | cut -d' ' -f1)"
        code=0
        ;;
    rclone)
        mounts="$("$here/rclone.sh" writes)"
        code=1
        expect=(
            "FAIL identity/truncate_grow_reads_zeros:"
            "FAIL inode/rename_keeps_inode:"
            "FAIL malice/mode_is_enforced:"
            "FAIL malice/rename_over_open:"
            "FAIL namespace/symlink_roundtrip:"
        )
        ;;
    samba)
        mounts="$("$here/samba.sh")"
        code=1
        expect=(
            "FAIL identity/utimes_survives_the_flush:"
            "FAIL namespace/hardlink:"
            "FAIL namespace/symlink_roundtrip:"
            "FAIL locks/unlock_splits_a_range:"
            "FAIL malice/rename_over_open:"
        )
        ;;
    knfsd-v3)
        mounts="$("$here/knfsd.sh" 3)"
        code=0
        ;;
    knfsd-v4.2)
        mounts="$("$here/knfsd.sh" 4.2)"
        code=1
        expect=("FAIL locks/cross_mount_excludes:")
        ;;
    ganesha-v4.1)
        mounts="$("$here/ganesha.sh" 4.1)"
        code=0
        ;;
    u9fs-none)
        mounts="$("$here/v9fs.sh" u9fs none 1)"
        code=1
        expect=(
            "FAIL locks/conflict_excludes:"
            "FAIL locks/exclusive_rmw:"
            "FAIL namespace/deep_rename_above:"
            "FAIL malice/unlink_open:"
            "shared.bin: file is"
        )
        ;;
    u9fs-mmap)
        mounts="$("$here/v9fs.sh" u9fs mmap 1)"
        code=1
        expect=(
            "observed.log: block (writer 0 seq"
            "shared.bin: file is"
        )
        ;;
    diod-none)
        mounts="$("$here/v9fs.sh" diod none 1)"
        code=1
        expect=(
            "FAIL locks/ranges_are_ranges:"
            "FAIL locks/getlk_names_the_holder:"
            "FAIL malice/unlink_open:"
            "block frame broken"
        )
        ;;
    diod-loose)
        mounts="$("$here/v9fs.sh" diod loose 1)"
        code=1
        expect=(
            "FAIL malice/dir_rename_open:"
            "FAIL namespace/deep_rename_above:"
            "shared.bin: file is"
        )
        ;;
    diod-mmap)
        mounts="$("$here/v9fs.sh" diod mmap 1)"
        code=1
        expect=("shared.bin: file is")
        ;;
    *)
        echo "unknown target: $target" >&2
        exit 2
        ;;
esac

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

expected=()
for line in "${expect[@]}"; do expected+=(--stderr "$line"); done
# SHOWCASE_MODE=record runs three times and prints what recurred, instead of asserting
mode=()
[ "${SHOWCASE_MODE:-assert}" = record ] && mode=(--record 3)
python3 "$here/expect.py" --exit "$code" --log "$log" "${mode[@]}" "${checks[@]}" "${expected[@]}" -- "${args[@]}"
