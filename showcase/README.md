# Showcase

fshammer run against filesystems you can mount on a GitHub Actions runner. Each target has a recorded
outcome: an exit code, and `FAIL` lines that appeared in every one of three runs. The
[Showcase workflow](../.github/workflows/showcase.yml) re-runs them on every push to `main` and weekly, and a
job fails if the outcome changes — including a target that starts passing.

Run one locally (Linux, sudo; it installs packages and mounts under `/tmp/showcase`):

```sh
scripts/showcase/run.sh davfs2
```

Every run uses `--duration 0 --procs 4 --seed 1`, a fresh directory under the mount, `--alt-dir` where the
target is mounted twice, and `--server-root` where the server applies writes before acknowledging them. The
setup for each target is in `scripts/showcase/<name>.sh`. Servers export a loop-mounted ext4 image.

Recorded in an `ubuntu:24.04` container (Python 3.12.3, kernel 6.12); the workflow runs on GitHub's
`ubuntu-24.04` runners, whose kernel differs. Running the workflow by hand with `mode: record` runs every target
three times and prints the `FAIL` lines common to all three without asserting anything, which is how a recorded
outcome is confirmed on the runner. Full logs are attached to each workflow run.

| Target | Version | Exit | Recorded `FAIL` lines |
|---|---|---|---|
| tmpfs, mounted twice (control) | kernel | 0 | none |
| davfs2 over apache2 mod_dav | davfs2 1.7.0, apache2 2.4.58 | 1 | 4 |
| davfs2, with its limits declared | as above | 1 | 2 |
| sshfs to local sshd, mounted twice | sshfs 3.7.3, OpenSSH 9.6p1 | 1 | 10 asserted (12 variants and `shared.bin`) |
| bindfs, mounted twice | bindfs 1.14.7 | 1 | 2 |
| bindfs, mounted once | bindfs 1.14.7 | 0 | none |
| rclone mount `--vfs-cache-mode writes` of a local directory | rclone v1.75.2 | 1 | 5 asserted (23 variants) |
| Samba, kernel SMB3 client, mounted twice | Samba 4.19.5 | 1 | 5 asserted (25 variants and `shared.bin`) |
| knfsd, NFSv3, mounted twice | kernel | 0 | none |
| knfsd, NFSv4.2, mounted twice | kernel | 1 | 1 |
| NFS-Ganesha, NFSv4.1, mounted twice | NFS-Ganesha 4.3 | 0 | none |
| 9p: u9fs, `cache=none` | u9fs (Plan 9, commit `fcec3e1`) | 1 | 5 asserted |
| 9p: u9fs, `cache=mmap` | as above | 1 | 2 asserted |
| 9p: diod, `cache=none` | diod 1.0.24 | 1 | 4 asserted |
| 9p: diod, `cache=loose` | as above | 1 | 3 asserted |
| 9p: diod, `cache=mmap` | as above | 1 | 1 asserted |

## davfs2

```sh
python3 fshammer.py --dir /tmp/showcase/dav/run --duration 0 --procs 4 --seed 1
```

- `identity/utimes_survives_the_flush`: a file is written, given an mtime with `utime()`, then closed.
  Read back straight after `close()`, it is empty (size 0). The content appears a few seconds later.
- `malice/unlink_open`: `fstat()` on a descriptor whose file has been unlinked fails with ENOENT.
- `namespace/hostile_names`, `namespace/long_names`: creating a file with a name of 250 bytes or longer
  fails with EIO. 248 bytes works.

Symlinks (ENOSYS), hardlinks (EPERM) and extended attributes (ENOTSUP) are refused with error codes that
fshammer accepts, so those variants are reported as `DECLARED GAP` rather than `FAIL`.

### With its limits declared

```sh
python3 fshammer.py --dir /tmp/showcase/dav/run --duration 0 --procs 4 --seed 1 \
    --max-name-length 248 --declare-gap malice/unlink_open
```

- `identity/utimes_survives_the_flush`: as above.
- `namespace/hostile_names`: the name one byte over the limit (249 bytes) is refused with EIO, where
  fshammer expects ENAMETOOLONG.

## sshfs, mounted twice

```sh
python3 fshammer.py --dir /tmp/showcase/sshfs-a/run --alt-dir /tmp/showcase/sshfs-b/run \
    --duration 0 --procs 4 --seed 1
```

- `inode/*`, `walk/walk_manifest`, `namespace/hardlink`: directory listings report inode 4294967295 for
  every entry while `stat()` reports another number, and a hardlink's two names report different inodes.
- `identity/ctime_is_not_just_mtime`: ctime always equals mtime. SFTP version 3 has no ctime.
- `identity/sentinel_mtimes`: an mtime of 0 is not stored.
- `namespace/hostile_names`: renaming a file to a 259-byte name fails with EBADMSG, where fshammer expects
  ENAMETOOLONG.
- `locks/cross_mount_excludes`: an `fcntl` lock taken through one mount does not exclude a lock taken
  through the other.
- `shared.bin`: a single large `O_APPEND` `write()` through one mount is interleaved with writes through the
  other, so a block is not stored contiguously. No bytes are lost. Through one mount this does not happen.

## bindfs, mounted twice

```sh
python3 fshammer.py --dir /tmp/showcase/bindfs-a/run --alt-dir /tmp/showcase/bindfs-b/run \
    --duration 0 --procs 4 --seed 1
```

- `shared.bin`: a single large `O_APPEND` `write()` through one mount is interleaved with writes through the
  other, so a block is not stored contiguously. No bytes are lost. With one bindfs mount, or two bind mounts
  of tmpfs, this does not happen.
- `locks/cross_mount_excludes`: as for sshfs.

## rclone mount

```sh
rclone mount /tmp/showcase/rclone-writes-remote /tmp/showcase/rclone-writes --vfs-cache-mode writes --daemon
python3 fshammer.py --dir /tmp/showcase/rclone-writes/run --duration 0 --procs 4 --seed 1
```

23 variants fail on every run. The five asserted:

- `malice/rename_over_open`: a file is renamed over another that is still open for writing. Afterwards the
  name holds 4097 bytes where the renamed file had 4096.
- `identity/truncate_grow_reads_zeros`: extending a file with `truncate()` fails with EIO.
- `namespace/symlink_roundtrip` and the other symlink variants: `symlink()` fails with EIO. rclone mount
  supports symlinks only with `--links`, which this run does not pass.
- `inode/rename_keeps_inode`: a rename changes the inode number.
- `malice/mode_is_enforced`: a file with mode 0400 can be opened for writing.

The other 18 are in the job log.

## Samba

```sh
mount -t cifs //127.0.0.1/share /tmp/showcase/samba-a -o vers=3.1.1,username=...,uid=...,gid=...
mount -t cifs //127.0.0.1/share /tmp/showcase/samba-b -o vers=3.1.1,...,nosharesock
python3 fshammer.py --dir /tmp/showcase/samba-a/run --alt-dir /tmp/showcase/samba-b/run \
    --server-root /tmp/showcase/samba-share/run --duration 0 --procs 4 --seed 1
```

The kernel client mounts with its defaults (`cache=strict`, `nounix`, `reparse=nfs`). Samba 4.19 as packaged in
Ubuntu 24.04 does not serve the SMB3 POSIX extensions, so this is SMB3 without them. Each of these was also
reproduced with a few lines of Python outside fshammer:

- `identity/utimes_survives_the_flush`: a file is written, its mtime set with `utime()` while the data is still
  buffered, then closed. After `close()` the mtime is the time of the close, not the time that was set.
- `namespace/hardlink`: `link()` succeeds and both names report `st_nlink` 2, but opening the second name fails
  with EINVAL.
- `namespace/symlink_roundtrip` and the other symlink variants: `symlink()` fails with EIO.

The remaining failures are differences between SMB and POSIX semantics: a file or directory that is open cannot
be renamed over or moved (EACCES), the name of an unlinked file that is still open cannot be reused, nor its
directory removed, until it is closed, byte-range locks are per handle and cannot be split, `\` cannot appear in
a name, directories report `st_nlink` 2 and `st_size` 0, and through two sessions concurrent `O_APPEND` writes are
interleaved, since the client computes the end of the file itself.

## knfsd

```sh
mount -t nfs -o vers=4.2 127.0.0.1:/ /tmp/showcase/knfsd-4.2-a
mount -t nfs -o vers=4.2,nosharecache 127.0.0.1:/ /tmp/showcase/knfsd-4.2-b
python3 fshammer.py --dir /tmp/showcase/knfsd-4.2-a/run --alt-dir /tmp/showcase/knfsd-4.2-b/run \
    --server-root /tmp/showcase/knfsd-export/run --duration 0 --procs 4 --seed 1
```

Over NFSv3 every run passes.

Over NFSv4.2, `locks/cross_mount_excludes` fails in every run: a process holds an exclusive `fcntl` lock on a
file through one mount, and a second process is then granted the same range through the other. Reproduced
outside fshammer with two Python processes. It happens with NFSv4.1 and 4.2 and not with 4.0 or 3, not when the
second mount shares the first one's cache (the default, without `nosharecache`), and not when the server has
leases disabled (`fs.leases-enable=0`), which stops it granting delegations.

Occasionally, not on every run, `shared.bin` loses bytes when appended to through both mounts. `open(2)`
documents that `O_APPEND` is not atomic across NFS clients, so this is not asserted.

## NFS-Ganesha

```sh
mount -t nfs -o vers=4.1 127.0.0.1:/export /tmp/showcase/ganesha-4.1-a
mount -t nfs -o vers=4.1,nosharecache 127.0.0.1:/export /tmp/showcase/ganesha-4.1-b
python3 fshammer.py --dir /tmp/showcase/ganesha-4.1-a/run --alt-dir /tmp/showcase/ganesha-4.1-b/run \
    --server-root /tmp/showcase/ganesha-export/run --duration 0 --procs 4 --seed 1
```

VFS FSAL, with numeric owners. Every run passes, including the cross-mount lock check.

## 9p

The Linux 9p client (v9fs) over TCP to a local server, one mount, `msize=1048576`, attached as the invoking
user. diod speaks 9P2000.L; u9fs speaks 9P2000.

```sh
mount -t 9p -o trans=tcp,port=564,aname=/tmp/showcase/v9fs-export,version=9p2000.L,uname=$USER,access=user,msize=1048576,cache=loose \
    127.0.0.1 /tmp/showcase/v9fs-diod-loose-a
python3 fshammer.py --dir /tmp/showcase/v9fs-diod-loose-a/run --duration 0 --procs 4 --seed 1
```

`--server-root` is passed only with `cache=none`, since with a page cache the client acknowledges writes the
server does not have yet.

Appends to one file from several processes through one mount, reproduced outside fshammer with 4 processes each
making 200 `O_APPEND` writes of 4096 bytes. Every `write()` returned the full length.

| Server | `cache=none` | `cache=loose` | `cache=mmap` |
|---|---|---|---|
| u9fs | 884736 of 3276800 bytes in the file | all 3276800 | all 3276800 |
| diod | all 3276800 | about 1 MB of 3276800 | about 1 MB of 3276800 |

With one process nothing is lost; with two, about half.

### u9fs, `cache=none`

- `shared.bin`: acknowledged appends are missing, as in the table above.
- `locks/conflict_excludes`, `locks/exclusive_rmw` and the other lock variants: two processes on the same mount
  are both granted exclusive `fcntl` locks on the same range. Reproduced outside fshammer.
- `malice/unlink_open`: `fstat()` on an open file that has been unlinked fails with ENOENT.
- `namespace/deep_rename_above`: after a directory above an open file is renamed, `fsync()` on the file fails
  with ENOENT.

Also failing, as differences of 9P2000 or u9fs from POSIX: no separate ctime, tab characters stored escaped as
`\09` (u9fs documents this), directories reporting `st_nlink` 2 and `st_size` 0, EXDEV when renaming a
directory into another one, EINVAL from `fsync()` on a directory, and EINVAL from a writable shared `mmap()`.

### u9fs, `cache=mmap`

- `observed.log`: one process appends while three others `stat()`, list and read the file's tail. Between 6% and
  30% of the acknowledged bytes are missing at the end. With `cache=loose`, nothing is missing.
- `shared.bin`: acknowledged appends are missing.

### diod, `cache=none`

- `shared.bin`: no bytes are missing, but some appended blocks are not stored contiguously.
- `locks/ranges_are_ranges`: a lock on a range next to a held one is refused, as though the whole file were
  locked. `locks/getlk_names_the_holder`: `F_GETLK` fails with EINVAL.
- `malice/unlink_open`: `fstat()` on an open file that has been unlinked fails with ENOENT.

### diod, `cache=loose`

- `shared.bin`: acknowledged appends are missing, as in the table above.
- `malice/dir_rename_open`, `namespace/deep_rename_above`: after a directory is renamed, files inside it cannot
  be opened under the new name (ENOENT), although the directory lists them. Reproduced with
  `mkdir d; echo hello > d/f; cat d/f; mv d d2; cat d2/f`; with `cache=none`, the last `cat` succeeds.

### diod, `cache=mmap`

- `shared.bin`: acknowledged appends are missing.

## Not included

- s3fs against a local S3 server (`rclone serve s3`): the mount root intermittently returned EIO on `stat()`,
  and it was not clear whether s3fs or the server was responsible.
- rclone mount `--vfs-cache-mode off`: every writer stops at its first open, which does not get past setup.
- NFS-Ganesha over NFSv3: in the container used for recording, the first `fcntl` lock request over NFSv3 did
  not return, with `rpc.statd` running. Whether this is Ganesha or the container setup was not established.
- Samba with the SMB3 POSIX extensions: Samba 4.19 as packaged in Ubuntu 24.04 does not serve them.
- rclone and davfs2 in front of WsgiDAV 4.3.5: the results largely repeat the rclone and davfs2 targets above.
- Two 9p mounts of one diod export: `open(O_CREAT)` without `O_EXCL` has been seen to fail with EEXIST, but
  rarely, and not in runs of this length.
