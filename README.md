# File System Hammer

[![CI](https://github.com/PacAnimal/fshammer/actions/workflows/ci.yml/badge.svg)](https://github.com/PacAnimal/fshammer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

fshammer runs a concurrent workload against a directory and checks the results. It is meant for filesystems
that sit between an application and a disk: NFS and SMB, FUSE daemons, WebDAV mounts, sync and caching
layers, overlay and encrypting filesystems.

- Each writer keeps an in-memory copy of each of its own files and applies every operation to both. At the
  end, every file's SHA-256 must match its copy.
- It covers appends (`O_APPEND`) and reopening files that are being appended to, renames and unlinks under
  open handles, symlinks and hardlinks, `fcntl` locks, `mmap`, extended attributes, directory listings
  under mutation, racing creates and deletes on one name, and ENOSPC.
- Every variant is attempted at least once, however short the run.
- Anything a run did not test is printed as a `DECLARED GAP` line with the reason.
- One Python file, standard library only, Python 3.12+. Linux and macOS.

> [!WARNING]
> Use a scratch filesystem. See [Disk usage](#disk-usage): a default run occupies up to about 1.5 GB, and
> if the filesystem reports less than 2 GiB free, one check fills it until writes fail with ENOSPC.

## Quick start

```sh
curl -LO https://raw.githubusercontent.com/PacAnimal/fshammer/main/fshammer.py
python3 fshammer.py --dir /mnt/under-test/fshammer --duration 0
```

`--duration 0` attempts every variant once and stops, which takes seconds to minutes. The default is a
300-second run.

To install it as a command:

```sh
pipx install git+https://github.com/PacAnimal/fshammer
fshammer --dir /mnt/under-test/fshammer
```

`--dir` must be empty or not exist (`lost+found` is ignored). After a run with no failures, everything in it
except `lost+found` is deleted, including anything another process put there during the run; `--keep`
leaves it. After a failed run nothing is deleted, and the directory must be emptied before the next run.

## Output

Results go to stdout: one or more lines per phase, `observed.log:` and `shared.bin:` lines, and a final
verdict. `FAIL` lines, `DECLARED GAP` lines and progress go to stderr.

```
own files: 44 checked, 126057567 bytes, all sha256 match
downloads: 20 completed as a real downloader does, 12 final files re-checked here, 76980080 bytes — chunked=4, oneshot=4, …
locks: 256 read-modify-writes under an exclusive lock, and the counter holds exactly that
malice: 73 completed across 19 variants — append_vs_truncate=4, atomic_replace=4, contended_exclusive=4, …
…
shared.bin: 814 blocks from 4 writers (4 via --dir, 0 via --alt-dir), 17262551 bytes acknowledged, 17262551 on disk
fshammer: OK (13s elapsed, 0s minimum requested)
```

```
  DECLARED GAP locks/cross_mount_excludes: no --alt-dir, so both processes would share one mount and the client's own kernel would answer instead of the store — so this shape is untested here
```

The `name=count` entries are the variants and how many times each completed. These names are what
`--declare-gap` takes.

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
| publish | a set of files made visible by switching one pointer (rename, symlink, sidecar), including fsync of the directory |
| inode | `(st_dev, st_ino)` is unique, and unchanged by rename, swap and subtree moves |
| walk | directory listings checked against the files they name; each file contains its own path |
| locks | `fcntl` locks: exclusion, shared locks, byte ranges, split unlocks, `F_GETLK`, release on close, and exclusion across two mounts |
| contest | two operations on one name back to back (mkdir then create, rmdir then create, …): only the first may succeed, and a delete must not remove what replaced it |
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
| `--seed N` | `1` | seed for random choices. With `--duration 0` and the same `--procs`, each writer makes the same choices; the interleaving between writers still varies |
| `--keep` | | keep the files after a run with no failures |
| `--alt-dir DIR` | | the same directory through a second mount. Odd-numbered writers append to `shared.bin` and `observed.log` through it, and the cross-mount lock and publish checks use it. Needs `--procs 2` or more |
| `--server-root DIR` | | where the server stores these files, if readable from this host. Used to tell a storage loss from a stale client cache. Only for a store that applies a write to that directory before acknowledging it; a write-behind store fails the publish ordering checks |
| `--crash-command CMD` | | shell command run after verification to make the store lose anything not yet durable. See [Crash testing](#crash-testing) |
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
`nosharecache` makes the second mount a separate client instance; without it, Linux shares one between
mounts of the same export, and the cross-mount checks test that one instance (fshammer reports this as a gap
when both paths have the same `st_dev`):

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

A davfs2 WebDAV mount, excluding what that client does not support:

```sh
python3 fshammer.py --dir /mnt/dav/fsh --duration 0 --max-name-length 200 --no-lock --no-map \
    --declare-gap shared --declare-gap inode --declare-gap malice/sparse_far_offset
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

[MIT](LICENSE)
