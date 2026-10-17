# File System Hammer

[![CI](https://github.com/PacAnimal/fshammer/actions/workflows/ci.yml/badge.svg)](https://github.com/PacAnimal/fshammer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/PacAnimal/fshammer/blob/main/LICENSE)

fshammer runs a concurrent workload against a directory and checks the results. It is meant for filesystems
that sit between an application and a disk: NFS and SMB, FUSE daemons, WebDAV mounts, sync and caching
layers, overlay and encrypting filesystems.

- Each writer keeps an in-memory copy of each of its own files and applies every operation to both. At the
  end, every file's SHA-256 must match its copy.
- It covers appends (`O_APPEND`) and reopening files that are being appended to, renames and unlinks under
  open handles, symlinks and hardlinks, `fcntl` locks, `mmap`, extended attributes, directory listings
  under mutation, racing creates and deletes on one name, and ENOSPC.
- Every variant runs at least once, however short the run. Anything a run could not test is printed as a
  `DECLARED GAP` line with the reason.
- One Python file, standard library only, Python 3.12+. Linux and macOS.

> [!WARNING]
> fshammer writes a lot of data: up to about 1.5 GB at once with the defaults, plus 4 GiB on a filesystem
> without sparse files. If the filesystem has less than 2 GiB free, it also keeps writing until writes are
> refused, on purpose, to test what happens when it runs out of space.
> While it is full, every other program writing to that filesystem gets "No space left on device".
> Don't point it at a filesystem your system or other users depend on. See [Disk usage](https://github.com/PacAnimal/fshammer#disk-usage).

## Quick start

```sh
curl -LO https://raw.githubusercontent.com/PacAnimal/fshammer/main/fshammer.py
python3 fshammer.py --dir /mnt/under-test/fshammer --duration 0
```

`--duration 0` attempts every variant once and stops, which takes seconds to minutes. The default is a
300-second run.

To install it as a command, from PyPI once it is published there, or from GitHub until then:

```sh
pipx install fshammer
pipx install git+https://github.com/PacAnimal/fshammer
fshammer --dir /mnt/under-test/fshammer
```

`--dir` must be empty or not exist (`lost+found` is ignored). After a run with no failures, everything in it
except `lost+found` is deleted, including anything another process put there during the run; `--keep`
leaves it. After a failed or interrupted run nothing is deleted, and the directory must be emptied before the
next run.

## Output

Results go to stdout: one or more lines per phase, `observed.log:` and `shared.bin:` lines, and a final
verdict. `FAIL` lines, `DECLARED GAP` lines and progress go to stderr.

```
own files: 44 checked, 126057567 bytes, all sha256 match
downloads: 20 completed as a real downloader does, 12 final files re-checked here, …
locks: 256 read-modify-writes under an exclusive lock, and the counter holds exactly that
malice: 72 completed across 19 variants — append_vs_truncate=4, atomic_replace=4, contended_exclusive=4, …
…
shared.bin: 809 blocks from 4 writers (4 via --dir, 0 via --alt-dir), 18240719 bytes acknowledged, …
fshammer: OK (10s elapsed, --duration 0)
```

```
  DECLARED GAP locks/cross_mount_excludes: no --alt-dir, so both processes would share one mount and the client's own kernel would answer instead of the store — so this shape is untested here
```

The `name=count` entries are the variants and how many times each completed. On the lines of the phases
listed under `--declare-gap`, these are the names it takes (the `sqlite-shaped:` line is the `sqlite`
phase).

When an own-file's final hash does not match, the `FAIL` line gives the file and both sizes. While writing,
random ranges are also read back, and a mismatch there reports the first differing offset, the operation
that wrote it, and whether the bytes were still wrong after the page cache was dropped.

| Exit | Meaning |
|---|---|
| `0` | no `FAIL` lines. Each `DECLARED GAP` line is something the run did not test |
| `1` | at least one `FAIL` line on stderr, or every writer died without reporting |
| `2` | invalid invocation: bad arguments, a non-empty `--dir`, or an `--alt-dir` that is not the same directory |

## Phases

| Phase | What it checks |
|---|---|
| own files | appends, 1-byte writes, multi-MiB writes, holes, truncates both ways, and writes that overwrite and extend at once, to files no other writer touches |
| download | a downloader's call sequence: placeholder file, `0600` `.part` file, 32 KiB buffered writes, a resumed reopen with `O_APPEND` and without `O_CREAT`/`O_TRUNC`, rename over the placeholder |
| malice | rename and unlink under open handles, atomic replace, writes past 4 GiB, readdir under mutation, log rotation, writers racing for one name, ENOSPC |
| identity | content changed while size and mtime stay the same; mtime and ctime behaviour |
| namespace | symlink cycles and dangling links, hardlinks, deep trees, subtree moves, case and Unicode normalisation, unusual and maximum-length names, extended attributes |
| sqlite | database-style I/O without SQLite: ordered two-file commits, size read back after each write, shared mappings read and written |
| publish | a set of files made visible by switching one pointer (rename, hard link into place, sidecar written first), including fsync of the directory |
| inode | `(st_dev, st_ino)` is unique, and unchanged by rename, swap and subtree moves |
| walk | directory listings checked against the files they name; each file contains its own path |
| locks | `fcntl` locks: exclusion, shared locks, byte ranges, split unlocks, `F_GETLK`, release on close, and exclusion across two mounts |
| contest | two operations on one name back to back. Two creates (mkdir then create, …): the second must fail with EEXIST. A delete then a create (rmdir then create, …): the delete must not remove what replaced it |
| observer | `stat`, `listdir` and reads of the tail of a file while another writer appends to it |
| shared | every writer appending to one file; each block describes itself, so loss, duplication and overwrites are found without knowing the order |

The ENOSPC check (`malice/enospc_is_honest`) runs once, after every writer has finished, so that other
writers are not hit by the full disk.

## Options

| Option | Default | Description |
|---|---|---|
| `--dir DIR` | required | directory to run in. Must be empty or absent |
| `--duration SECONDS` | `300` | total run time, split between phases in fixed shares. The first pass of every variant always completes, even past this; `0` = one pass. A run can end early if `shared.bin` reaches its size cap |
| `--procs N` | `4` | concurrent writer processes. With 1, the contended variants and cross-writer checks are reported as gaps |
| `--seed N` | `1` | seed for random choices. With `--duration 0` and the same `--procs`, each writer makes the same choices; how many operations run, and their interleaving, still depend on timing |
| `--keep` | | keep the files after a run with no failures |
| `--version` | | print the version and exit |
| `--alt-dir DIR` | | the same directory through a second mount. Odd-numbered writers append to `shared.bin` and watch `observed.log` through it, and the cross-mount lock and publish checks use it. Needs `--procs 2` or more |
| `--server-root DIR` | | where the server stores these files, if readable from this host. Used to tell a storage loss from a stale client cache. Only for a store where a file fsynced through the mount, with its directory fsynced, is immediately visible there with that content; a store that acknowledges fsync before applying it fails the publish checks |
| `--crash-command CMD` | | shell command run after verification to make the store lose anything not yet durable. See [Crash testing](https://github.com/PacAnimal/fshammer#crash-testing) |
| `--pre-audit-command CMD` | | shell command run after the writers finish and before verification starts, to inject faults the verification itself should survive. A non-zero exit is a `FAIL` |
| `--fill-budget BYTES` | 2 GiB | most the ENOSPC check writes (plus up to 64 MiB). It runs only when `statvfs` reports less free space than this |
| `--single-writer-handle` | | one write descriptor per own-file, and `pwrite` at end-of-file instead of `O_APPEND` for positional writes, for clients that cache writes. Reported as a gap |
| `--max-name-length BYTES` | `255` | longest path component to create, 16 to 255, for stores that reserve part of a name. Length checks run at this limit instead; the difference is reported as a gap |
| `--no-map` | | skip the `mmap` variants. Reported as a gap |
| `--no-lock` | | skip the locks phase. Reported as a gap |
| `--declare-gap PHASE[/VARIANT]` | | skip a phase or one variant, reported as a gap. Repeatable. Phases: `contest`, `identity`, `inode`, `locks`, `malice`, `namespace`, `publish`, `shared`, `sqlite`, `walk`. An unknown phase is rejected; a variant name that matches nothing is a `FAIL` |

The skip options reduce what is tested. Verification is unaffected: every own-file is still read back and
hashed.

## Examples

One pass on a local filesystem:

```sh
python3 fshammer.py --dir /mnt/scratch/fsh --duration 0
```

An NFS export mounted twice, with the server's directory readable from the client, for an hour.
`nosharecache` gives the second mount its own data and attribute caches; without it, both paths share one
cache and the same `st_dev`, which fshammer reports as a gap. Either way both mounts share one NFS client
(one NFSv4 client ID and set of delegations), so the server sees one client. A two-client test needs a
second machine or network namespace:

```sh
mount -t nfs -o vers=3 server:/export /mnt/a
mount -t nfs -o vers=3,nosharecache server:/export /mnt/b
python3 fshammer.py --dir /mnt/a/fsh --alt-dir /mnt/b/fsh \
    --server-root /srv/export/fsh --duration 3600 --procs 8
```

Durability across a server restart (`systemctl kill` returns before the process has exited, so wait for it):

```sh
python3 fshammer.py --dir /mnt/a/fsh --crash-command 'ssh server "
    systemctl kill -s KILL my-fs-server
    while systemctl is-active -q my-fs-server; do sleep 0.2; done
    systemctl start my-fs-server"'
```

ENOSPC behaviour, on a filesystem with less than 2 GiB free:

```sh
python3 fshammer.py --dir /mnt/small/fsh --duration 0
```

A davfs2 WebDAV mount. davfs2 names its local cache files after the entry plus a 7-byte suffix, so names
over 248 bytes fail; `sparse_far_offset` is skipped because davfs2 would upload the whole 4 GiB file. This
still exits 1 on davfs2 1.7.3 (see [showcase/README.md](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md)):

```sh
python3 fshammer.py --dir /mnt/dav/fsh --duration 0 --max-name-length 248 \
    --declare-gap malice/unlink_open --declare-gap malice/sparse_far_offset
```

## Disk usage

At peak, a run holds up to 256 MiB per writer in `shared.bin`, 128 MiB in `observed.log`, and about 250 MB
of other files: about 1.5 GB with the default 4 writers. It writes several times that in total over a
300-second run, mostly through repeated downloads.

- `malice/sparse_far_offset` writes past the 4 GiB offset. On a filesystem without sparse files that
  allocates 4 GiB; the variant is skipped as a gap when less than 5 GiB is free.
- The ENOSPC check runs when `statvfs` reports less free space than `--fill-budget`, and then writes until
  the filesystem refuses, up to the budget plus 64 MiB. Anything else using that filesystem sees it full
  until the check deletes its filler. A mount that reports little or no free space when it has plenty gets
  the full budget written to it.

## Crash testing

`--crash-command` runs once, after verification, and must leave the filesystem mounted at the same path. It
is skipped if the run has already failed.

Before running it, fshammer fsyncs every own-file and completed download, the directories containing them,
and the directory containing `--dir` (with `F_FULLFSYNC` on macOS). Afterwards it reads those files back and
compares hashes, so a missing or changed file was fsynced and then lost. `shared.bin`, `observed.log` and the
other phases' files are not re-checked.

This is one crash at one point. Tools that record and replay block-level writes, such as CrashMonkey, explore
many more crash states.

## Platforms

Linux is the main target. Before verification reads, fshammer asks the kernel to drop cached pages with
`posix_fadvise(POSIX_FADV_DONTNEED)`. That is best effort: it does not drop dirty or mapped pages, or caches
inside a FUSE daemon or a server. macOS has no `posix_fadvise`, so reads there may be answered from the page
cache, and the run reports that as a gap. Windows is not supported.

Run it as an ordinary user. Root bypasses permission checks, so `malice/mode_is_enforced` is reported as a
gap.

## Limits

- It observes one host through one directory (two with `--alt-dir`). It cannot check that two machines
  agree.
- It checks results, not speed. A correct answer that took a minute passes.
- Many checks are races between writers, so a clean run does not show they cannot fail. A longer
  `--duration` gives them more chances.

## Showcase

[![Showcase](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml/badge.svg)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml)

fshammer against filesystems set up on a GitHub runner, re-run weekly and when fshammer or the showcase
changes. Where it was practical the software is built from its latest release. Each badge describes the latest
complete run: green `no failures`; yellow `N failing checks (documented)` when every failing check is a
documented limitation; orange `N failing checks` when any is a likely defect or not yet explained; red when the
outcome differs from the record or the run did not complete. `N` counts distinct failing checks. What each
failure is, which component it points at, and a standalone reproducer for each likely defect, are in
[showcase/README.md](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md).

Green means one pass (`--duration 0`) found nothing, on one host over loopback, with a second mount that is the
same client or a bind mount, and without filling the filesystem or testing a crash. It is not a
certification.

<!-- showcase-table -->
| Target | Status | Failing checks |
|---|---|---|
| tmpfs, mounted twice (control) | [![tmpfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/tmpfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| ext4 on a loop image, bind-mounted twice | [![ext4](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ext4.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| XFS on a loop image, bind-mounted twice | [![xfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/xfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| Btrfs on a loop image, bind-mounted twice | [![btrfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/btrfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| overlayfs over ext4, mounted twice | [![overlayfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/overlayfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| [knfsd, NFSv3, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#knfsd) | [![knfsd-v3](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/knfsd-v3.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | documented: 1 check, in some runs. |
| [knfsd, NFSv4.2, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#knfsd) | [![knfsd-v4.2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/knfsd-v4.2.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 1 check, in some runs; documented: 1 check, in some runs. |
| [knfsd, NFSv4.2, two clients](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#knfsd) | [![knfsd-v4.2-2client](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/knfsd-v4.2-2client.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| [NFS-Ganesha, NFSv3, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha) | [![ganesha-v3](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-v3.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| [NFS-Ganesha, NFSv4.0, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha) | [![ganesha-v4.0](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-v4.0.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| [NFS-Ganesha, NFSv4.1, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha) | [![ganesha-v4.1](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-v4.1.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 1 check, in some runs. |
| [NFS-Ganesha, NFSv4.2, mounted twice on one client](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha) | [![ganesha-v4.2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-v4.2.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 1 check, in some runs; documented: 1 check, in some runs. |
| [NFS-Ganesha, NFSv4.2, two clients](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha) | [![ganesha-v4.2-2client](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-v4.2-2client.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 1 check, in some runs; documented: 1 check, in some runs. |
| [Samba, Linux SMB3 client, mounted twice](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#samba) | [![samba](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/samba.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 8 checks; documented: 4 checks. |
| [davfs2 over Apache mod_dav](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#davfs2) | [![davfs2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/davfs2.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 1 check (`identity/utimes_survives_the_flush`); not established: 3 checks. |
| [davfs2, Ubuntu's package](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#davfs2-170-ubuntu-2404) | [![davfs2-ubuntu](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/davfs2-ubuntu.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 1 check (`identity/utimes_survives_the_flush`); follows from another failure: 22 checks (+1 in some runs); not established: 3 checks. |
| [sshfs, mounted twice](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#sshfs) | [![sshfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/sshfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 3 checks; documented: 16 checks. |
| [bindfs, mounted once](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#bindfs) | [![bindfs-single](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/bindfs-single.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | None in the recorded runs. |
| [bindfs, mounted twice](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#bindfs) | [![bindfs](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/bindfs.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | not established: 1 check; documented: 1 check. |
| [rclone mount, `--vfs-cache-mode writes`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#rclone) | [![rclone](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/rclone.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 2 checks (`identity/truncate_grow_reads_zeros`, `malice/rename_over_open`); not established: 8 checks; documented: 3 checks. |
| [9p: u9fs, `cache=none`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#u9fs-9p2000) | [![u9fs-none](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/u9fs-none.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 6 checks; not established: 3 checks (+1 in some runs); documented: 4 checks. |
| [9p: u9fs, `cache=mmap`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#u9fs-9p2000) | [![u9fs-mmap](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/u9fs-mmap.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 7 checks (+1 in some runs); not established: 3 checks; documented: 4 checks. |
| [9p: diod, `cache=none`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#diod-9p2000l) | [![diod-none](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/diod-none.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 2 checks (`malice/log_rotation`, `malice/rename_live`); documented: 4 checks (+3 in some runs). |
| [9p: diod, `cache=loose`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#diod-9p2000l) | [![diod-loose](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/diod-loose.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 3 checks (`inode/three_rename_swap`, `malice/log_rotation`, `malice/rename_recreate`); not established: 1 check; documented: 3 checks (+3 in some runs). |
| [9p: diod, `cache=mmap`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#diod-9p2000l) | [![diod-mmap](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/diod-mmap.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | likely defect: 2 checks (+1 in some runs) (`malice/log_rotation`, `malice/rename_live`, `observed.log`); not established: 1 check; documented: 3 checks (+2 in some runs). |
| [9p: NFS-Ganesha, `cache=none`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha-9p2000l) | [![ganesha-9p-none](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-9p-none.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | follows from another failure: 15 checks. |
| [9p: NFS-Ganesha, `cache=loose`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha-9p2000l) | [![ganesha-9p-loose](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-9p-loose.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | follows from another failure: 15 checks. |
| [9p: NFS-Ganesha, `cache=mmap`](https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md#nfs-ganesha-9p2000l) | [![ganesha-9p-mmap](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/ganesha-9p-mmap.json)](https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml) | follows from another failure: 15 checks. |
<!-- /showcase-table -->

## Related tools

| Tool | What it does | Compared with fshammer |
|---|---|---|
| [xfstests](https://git.kernel.org/pub/scm/fs/xfs/xfstests-dev.git) | the regression suite for Linux kernel filesystems, including NFS, CIFS, overlayfs and FUSE; includes fsx and fsstress | far broader. Needs configuring per filesystem, with a scratch filesystem for some tests |
| fsx (in xfstests) | random reads, writes, truncates, mappings, fallocate, hole punching, clone and copy ranges, AIO and direct I/O on one file, checked against an in-memory copy | the same kind of check, on one file from one process, with many more I/O types. Never opens a file with `O_APPEND` |
| fsstress (in xfstests) | random namespace and I/O operations from many processes | does not check file contents. Never opens a file with `O_APPEND` |
| [LTP](https://github.com/linux-test-project/ltp) growfiles | grows files, optionally with `O_APPEND`, and checks their contents | sizes its work from `statfs` free space, so it does little on a mount that reports none |
| [Connectathon](https://git.linux-nfs.org/?p=steved/cthon04.git) (cthon04) | NFS client tests: basic operations, special cases, and byte-range locking | the standard NFS interoperability tests. Fewer concurrent and data-checking tests |
| smbtorture (in Samba) | SMB protocol tests, run against a server directly | tests the protocol, not a mounted filesystem |
| [pjdfstest](https://github.com/pjd/pjdfstest) | POSIX conformance of individual syscalls: return values, errno, permissions | far more error cases. fshammer checks data and concurrent behaviour |
| [fio](https://github.com/axboe/fio) | I/O benchmark | measures performance. Its `verify` option checks data written by its own jobs |
| [CrashMonkey](https://github.com/utsaslab/crashmonkey) | crash consistency, by recording and replaying block-level writes | many more crash states, but needs a block device it controls |

## Development

```sh
python3 -m pytest                          # unit tests; the reference-filesystem tests are skipped
eval "$(scripts/reference-fs.sh up)"       # Linux and sudo: ext4 loop mounts, a bind mount, a small filesystem
python3 -m pytest                          # everything
scripts/reference-fs.sh down
```

The self-tests run fshammer against ext4 and require every variant to complete. The negative controls make
it look at a filesystem that has lost a file, corrupted a file or made a file unreadable, and at a mutated
copy of fshammer that answers a race wrongly, and require each to fail.

CI runs the unit tests on Linux and macOS with Python 3.12, 3.13 and 3.14, and the reference-filesystem
tests on Linux, on pushes to `main` and on pull requests.

## License

[MIT](https://github.com/PacAnimal/fshammer/blob/main/LICENSE)
