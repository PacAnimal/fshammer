# Showcase

fshammer run against filesystems that can be set up on a GitHub Actions runner, with a standalone
reproducer for each behaviour listed below as a likely defect. Everything here was recorded on GitHub's
`ubuntu-24.04` runner, kernel `6.17.0-1022-azure`, Python 3.12.

## What the workflow checks

The [Showcase workflow](../.github/workflows/showcase.yml) runs every target and every reproducer on each
push to `main`, weekly, and on demand.

- **A target job** runs fshammer once (`--duration 0 --procs 4 --seed 1`) and compares its `FAIL` lines with
  [`scripts/showcase/expected/<target>.txt`](../scripts/showcase/expected). A `FAIL` line is compared up to
  its first `: `, with a path reduced to its file name and digits replaced by `N`. The job fails if fshammer
  does not finish, its exit code does not match whether it printed `FAIL` lines, a listed line is missing,
  or an unlisted one appears. Lines marked `?` appeared in some recorded runs and not others; they are
  allowed but not required.
- **A reproducer job** runs a script from [`scripts/showcase/repro/`](../scripts/showcase/repro) that sets up
  the target and makes a few system calls, without fshammer. It passes while the behaviour reproduces and
  fails once it no longer does.
- Running the workflow by hand with `mode: record` runs each target three times and writes a proposed
  expectations file instead of asserting.

## Running a target yourself

```sh
scripts/showcase/run.sh samba                  # one target
scripts/showcase/repro/samba-hardlink.sh       # one reproducer
```

Only in a throwaway VM or container, with sudo. The setup scripts install packages, overwrite
`/etc/exports` and `/etc/samba/smb.conf`, add an Apache configuration, append to `/etc/davfs2/secrets`, add
an SSH key to `~/.ssh/authorized_keys`, set a Samba password for the current user, change NFS server grace
and lease times, start servers, and leave everything mounted and running. Nothing is cleaned up.

Samba, knfsd, NFS-Ganesha and the 9p servers export a loop-mounted ext4 image. sshfs, bindfs, rclone and
Apache serve a plain directory on the runner's own filesystem.

## Results

| Target | Version | `FAIL` lines | Reproducers |
|---|---|---|---|
| tmpfs, mounted twice (control) | kernel | 0 | |
| [knfsd, NFSv3](#knfsd-and-nfs-ganesha), mounted twice | nfs-kernel-server 2.6.4 | 0, 1 optional | |
| [knfsd, NFSv4.2](#knfsd-and-nfs-ganesha), mounted twice | nfs-kernel-server 2.6.4 | 0, 2 optional | 1 |
| [NFS-Ganesha, NFSv4.1](#knfsd-and-nfs-ganesha), mounted twice | nfs-ganesha 4.3 | 0, 1 optional | |
| [Samba](#samba), Linux SMB3 client, mounted twice | samba 4.19.5, cifs-utils 7.0 | 12, 1 optional | 2 |
| [davfs2](#davfs2) over Apache mod_dav | davfs2 1.7.0, apache2 2.4.58 | 26 | 2 |
| [sshfs](#sshfs), mounted twice | sshfs 3.7.3, OpenSSH 9.6p1 | 13 | 1 |
| [bindfs](#bindfs), mounted once | bindfs 1.14.7, fuse3 3.14.0 | 1 | 1 |
| [bindfs](#bindfs), mounted twice | as above | 3 | |
| [rclone mount](#rclone) of a local directory | rclone v1.75.2 | 13, 1 optional | 2 |
| [9p: u9fs](#9p), `cache=none` | u9fs at `fcec3e1` | 13, 2 optional | 4 |
| [9p: u9fs](#9p), `cache=mmap` | as above | 14, 1 optional | 1 |
| [9p: diod](#9p), `cache=none` | diod 1.0.24 | 6, 2 optional | 1 |
| [9p: diod](#9p), `cache=loose` | as above | 10, 2 optional | 2 |
| [9p: diod](#9p), `cache=mmap` | as above | 5, 3 optional | 1 |

Each failure below is one of:

- **Likely defect**: reproduced without fshammer and not explained by any documentation found. The
  component named is the one the evidence points at; where that is not established, it says so.
- **Limitation**: documented, or inherent in the protocol.
- **Not established**: observed, with no conclusion yet about whether it is a defect.

A failure caused by another one in the same section is noted as such.

## knfsd and NFS-Ganesha

The export is mounted twice from the same host, the second time with `nosharecache`. Both mounts share one
NFS client (one NFSv4 client ID and one set of delegations), so the server sees a single client.

```sh
echo "/srv/export 127.0.0.1(rw,sync,no_subtree_check,no_root_squash,fsid=0)" > /etc/exports
mount -t nfs -o vers=4.2 127.0.0.1:/ /mnt/a
mount -t nfs -o vers=4.2,nosharecache 127.0.0.1:/ /mnt/b
```

- **Likely defect, component not established (knfsd, NFSv4.2):** while one process holds an exclusive
  `fcntl` lock through `/mnt/a`, another process is granted the same lock through `/mnt/b`. Reproduced every
  time by [`knfsd-v4-lock-two-mounts.sh`](../scripts/showcase/repro/knfsd-v4-lock-two-mounts.sh); fshammer's
  `locks/cross_mount_excludes` catches it in some runs. It does not happen with NFSv3, or with NFS-Ganesha,
  whose delegations are off by default. Because both mounts are one client, this may be the client
  handling the lock locally under a delegation; repeating it with the second mount on another host or in
  another network namespace would tell client and server apart.
- **Limitation:** `shared.bin` sometimes loses appends made through both mounts. `open(2)` documents that
  `O_APPEND` on NFS is not atomic when more than one process appends.

## Samba

Samba serving an ext4 directory, mounted twice with the Linux SMB3 client, the second mount with
`nosharesock` (a separate session). Ubuntu's Samba 4.19 does not serve the SMB3 POSIX extensions, so this is
plain SMB3.

```sh
mount -t cifs //127.0.0.1/share /mnt/a -o vers=3.1.1,username=$USER,password=PASSWORD,uid=$(id -u),gid=$(id -g)
mount -t cifs //127.0.0.1/share /mnt/b -o vers=3.1.1,username=$USER,password=PASSWORD,uid=$(id -u),gid=$(id -g),nosharesock
```

- **Likely defect, client or server not established:** after `link(a, b)` both names report `st_nlink` 2,
  and opening `b` fails with EINVAL (`namespace/hardlink`).
  [`samba-hardlink.sh`](../scripts/showcase/repro/samba-hardlink.sh):

  ```python
  open("/mnt/a/x", "wb").write(b"hello")
  os.link("/mnt/a/x", "/mnt/a/y")
  open("/mnt/a/y", "rb")            # OSError: [Errno 22] Invalid argument
  ```

- **Likely defect, client or server not established:** an mtime set with `utime()` while written data is
  still buffered is replaced by the time of the `close()` (`identity/utimes_survives_the_flush`).
  [`samba-utime-before-close.sh`](../scripts/showcase/repro/samba-utime-before-close.sh):

  ```python
  fd = os.open("/mnt/a/t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
  os.write(fd, os.urandom(40960))
  os.utime("/mnt/a/t", (time.time() - 86400,) * 2)
  os.close(fd)
  os.stat("/mnt/a/t").st_mtime      # the time of the close, not a day ago
  ```

- **Limitation:** without the POSIX extensions the client reports modes from its `file_mode` and `dir_mode`
  mount options (mount.cifs(8)), and the server checks access as the mounting user, so `chmod` does not
  stick and mode 0000 is not enforced (`identity/chmod_keeps_bytes_and_mtime`, `malice/mode_is_enforced`).
- **Limitation:** an SMB byte-range lock belongs to an open handle rather than a process, and an unlock must
  match a lock exactly, so closing another descriptor does not drop a lock and a range cannot be split
  (`locks/close_drops_every_lock`, `locks/unlock_splits_a_range`).
- **Limitation:** a directory containing an open file cannot be renamed (EACCES)
  (`malice/dir_rename_open`, `namespace/deep_rename_above`).
- **Limitation:** a name cannot contain a backslash (`namespace/hostile_names`).
- **Limitation:** directories report `st_nlink` 2 and size 0 whatever they hold
  (`inode/directory_link_count_is_usable`, `inode/directory_size_is_usable`).
- **Limitation:** `shared.bin` loses appends made through the two sessions; `O_APPEND` is not atomic across
  SMB clients either.
- **Not established:** in some runs, `rmdir` of a directory whose files have all been unlinked fails with
  ENOTEMPTY (`malice/fd_survives_enumeration`, optional).

## davfs2

davfs2 mounting Apache mod_dav on the same host.

```sh
# Apache: Alias /dav /var/www/dav, and "Dav On" for that directory
echo '/mnt/dav "" ""' >> /etc/davfs2/secrets
mount -t davfs http://127.0.0.1/dav /mnt/dav -o uid=$(id -u),gid=$(id -g)
```

- **Likely defect (davfs2 on this kernel):** every directory listing fails with EINVAL; reading files works.
  davfs2 answers EINVAL to a `FUSE_READ` or `FUSE_READDIR` request larger than its own buffer (`dav_fuse.c`:
  `if (in->size > (buf_size - sizeof(struct fuse_out_header)))`), and this kernel asks for more than that
  when reading a directory. On kernel 6.12 the same setup lists directories. davfs2 1.7.0, from 2022, is the
  latest release. [`davfs2-readdir.sh`](../scripts/showcase/repro/davfs2-readdir.sh):

  ```python
  os.mkdir("/mnt/dav/d"); open("/mnt/dav/d/f", "w").close()
  os.listdir("/mnt/dav/d")          # OSError: [Errno 22] Invalid argument
  ```

- **Likely defect (davfs2):** a file written, given an mtime with `utime()`, and closed reads back empty
  straight after `close()` (`identity/utimes_survives_the_flush`).
  [`davfs2-utime-before-close.sh`](../scripts/showcase/repro/davfs2-utime-before-close.sh):

  ```python
  fd = os.open("/mnt/dav/u", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
  os.write(fd, os.urandom(4096))
  os.utime("/mnt/dav/u", (time.time() - 86400,) * 2)
  os.close(fd)
  open("/mnt/dav/u", "rb").read()   # b''
  ```

- **Limitation:** an open file that has been unlinked cannot be `fstat`ed (ENOENT); WebDAV has no notion of
  an open file (`malice/unlink_open`).
- The other 24 lines, including `writer N` (three of the four writers stop at their first directory
  listing), are caused by the listing failure.

Symlinks (ENOSYS), hard links (EPERM) and extended attributes (ENOTSUP) are refused with errors fshammer
accepts, so those variants are reported as gaps. On kernel 6.12, where listings work, names longer than 248
bytes also fail with EIO: davfs2 keeps a local cache file named after the entry plus a 7-byte suffix.

## sshfs

sshfs to the local sshd, mounted twice.

```sh
sshfs $USER@127.0.0.1:/srv/export /mnt/a
sshfs $USER@127.0.0.1:/srv/export /mnt/b
```

- **Likely defect (sshfs):** a directory listing reports inode 4294967295 for every entry, while `stat()`
  reports another number (`inode/readdir_agrees_under_mutation`, `walk/walk_manifest`).
  [`sshfs-readdir-inode.sh`](../scripts/showcase/repro/sshfs-readdir-inode.sh):

  ```python
  os.mkdir("/mnt/a/d"); open("/mnt/a/d/f", "w").close()
  next(os.scandir("/mnt/a/d")).inode(), os.stat("/mnt/a/d/f").st_ino   # (4294967295, 3)
  ```

  The other `inode/*` lines are caused by this.
- **Not established:** after `link(a, b)` the two names report different inode numbers from `stat()`
  (`namespace/hardlink`).
- **Not established:** `utime(path, (0, 0))` leaves the mtime at the current time
  (`identity/sentinel_mtimes`), and a 256-byte name fails with EBADMSG rather than ENAMETOOLONG
  (`namespace/hostile_names`).
- **Limitation:** SFTP version 3 has no ctime, so ctime equals mtime (`identity/ctime_is_not_just_mtime`).
- **Limitation:** sshfs does not implement locking, so locks are kept by the kernel per mount and a lock
  through one mount does not exclude the other (`locks/cross_mount_excludes`). libfuse documents that
  without a lock method "the kernel will still allow file locking to work locally".
- **Not established:** in `shared.bin`, appended through both mounts, blocks are overwritten by appends
  made through the other mount. Through one mount this does not happen.

## bindfs

bindfs of a local directory, mounted once and twice.

```sh
bindfs --no-allow-other /srv/src /mnt/a
bindfs --no-allow-other /srv/src /mnt/b
```

- **Likely defect (bindfs):** after `link(a, b)` and a read through `b`, data appended through `a` is not
  seen when `b` is read again, though both names report the same inode (`namespace/hardlink`).
  [`bindfs-hardlink.sh`](../scripts/showcase/repro/bindfs-hardlink.sh): after an 8192-byte write, the link,
  and a 1024-byte append through the first name, the second name reads 8192 bytes.
- **Limitation (two mounts):** as for sshfs, locks are per mount (`locks/cross_mount_excludes`).
- **Not established (two mounts):** as for sshfs, blocks in `shared.bin` are overwritten by appends made
  through the other mount.

## rclone

`rclone mount` of a local directory with `--vfs-cache-mode writes` (without a cache mode rclone can only
write files sequentially) and `--links` (symlinks, off by default).

```sh
rclone mount /srv/remote /mnt/r --vfs-cache-mode writes --links --cache-dir /srv/cache --daemon
```

- **Likely defect (rclone):** extending a file with `truncate()` fails with EIO
  (`identity/truncate_grow_reads_zeros`).
  [`rclone-truncate-grow.sh`](../scripts/showcase/repro/rclone-truncate-grow.sh):

  ```python
  open("/mnt/r/g", "wb").write(b"x" * 100)
  os.truncate("/mnt/r/g", 4096)     # OSError: [Errno 5] Input/output error
  ```

- **Likely defect (rclone):** when a file is renamed over another that is held open with `O_APPEND`, a write
  made afterwards through the held descriptor lands in the file that replaced it (`malice/rename_over_open`).
  [`rclone-rename-over-open.sh`](../scripts/showcase/repro/rclone-rename-over-open.sh):

  ```python
  open("/mnt/r/dst", "wb").write(b"V" * 4096)
  open("/mnt/r/src", "wb").write(b"S" * 4096)
  held = os.open("/mnt/r/dst", os.O_WRONLY | os.O_APPEND)
  os.rename("/mnt/r/src", "/mnt/r/dst")
  os.write(held, b"G"); os.close(held)
  len(open("/mnt/r/dst", "rb").read())   # 4097
  ```

- **Limitation:** rclone does not store permissions; it reports `--file-perms` (default 0666) narrowed by
  the umask, so `chmod` does not stick and modes are not enforced (`identity/chmod_keeps_bytes_and_mtime`,
  `malice/mode_is_enforced`, `namespace/symlink_into_mount`).
- **Not established:** ctime equals mtime (`identity/ctime_is_not_just_mtime`); a rename gives the file a new
  inode number (`inode/rename_keeps_inode` and four other `inode/*` lines); directories report size 0
  (`inode/directory_size_is_usable`); a 256-byte name fails with EIO rather than ENAMETOOLONG
  (`namespace/hostile_names`); in some runs, `observed.log` has blocks overwritten while it is appended to
  and read (optional).

## 9p

The Linux 9p client over TCP to u9fs (Plan 9's Unix 9P server, built from
[github.com/Plan9-Archive/u9fs](https://github.com/Plan9-Archive/u9fs) at `fcec3e1`, speaking 9P2000) and to
diod (9P2000.L), each exporting an ext4 directory, with a 1 MiB `msize`.

```sh
# u9fs, one process per connection, as inetd would run it
socat TCP-LISTEN:5640,bind=127.0.0.1,reuseaddr,fork "EXEC:u9fs -a none -m 1048576 /srv/export" &
mount -t 9p -o trans=tcp,port=5640,aname=/srv/export,version=9p2000,uname=$USER,access=user,msize=1048576,cache=none,dfltuid=$(id -u),dfltgid=$(id -g) 127.0.0.1 /mnt/9

# diod
diod --no-auth --export /srv/export --listen 127.0.0.1:564
mount -t 9p -o trans=tcp,port=564,aname=/srv/export,version=9p2000.L,uname=$USER,access=user,msize=1048576,cache=loose,dfltuid=$(id -u),dfltgid=$(id -g) 127.0.0.1 /mnt/9
```

### Both servers

- **Likely defect (Linux 9p client):** `fstat()` on an open file that has been unlinked fails with ENOENT,
  with u9fs and with diod (`malice/unlink_open`).
  [`9p-unlink-open.sh`](../scripts/showcase/repro/9p-unlink-open.sh):

  ```python
  fd = os.open("/mnt/9/f", os.O_RDWR | os.O_CREAT, 0o644)
  os.unlink("/mnt/9/f")
  os.fstat(fd)                      # FileNotFoundError: [Errno 2]
  ```

- **Limitation:** a shared writable mapping is refused (EINVAL) with u9fs in both cache modes and with diod
  in `cache=none` (`sqlite/mmap_write_through`).

### u9fs (9P2000)

- **Likely defect (Linux 9p client):** two processes on one mount are both granted an exclusive `fcntl` lock
  on the same range (`locks/*`). 9P2000 has no lock messages, and the client's `v9fs_file_lock` returns
  success without recording the lock. [`9p-locks-9p2000.sh`](../scripts/showcase/repro/9p-locks-9p2000.sh).
- **Likely defect (Linux 9p client):** renaming a directory at the top of the export, with a file open
  beneath it, fails with errno 526. That is ESERVERFAULT, which is defined only in the kernel's internal
  `errno.h`, so programs see "Unknown error 526"; the client returns it when the server's error string is not
  in its table, and logs the string. The same rename one level down succeeds.
  [`9p-rename-errno.sh`](../scripts/showcase/repro/9p-rename-errno.sh).
- **Likely defect (Linux 9p client, `cache=mmap`):** a file appended to while another process calls `stat()`
  on it loses bytes `write()` reported (`observed.log`, `shared.bin`).
  [`9p-stat-during-append.sh`](../scripts/showcase/repro/9p-stat-during-append.sh): 400 writes of 32 KiB,
  13107200 bytes reported written, 5177344 in the file.
- **Limitation:** four processes appending to one file with `O_APPEND` on one mount lose bytes. 9P2000 has no
  append flag, and the client moves each descriptor to the end of the file once, when it is opened.
  [`9p-append.sh u9fs none`](../scripts/showcase/repro/9p-append.sh): 3276800 bytes reported written, 819200
  in the file. fshammer's `shared.bin` and `observed.log` show this in some runs.
- **Not established:** a descriptor held twelve directories below a renamed directory fails `fsync()` with
  ENOENT (`namespace/deep_rename_above`).
- **Limitation:** 9P2000 has no ctime and no link count, so ctime equals mtime and directories report
  `st_nlink` 2 and size 0 (`identity/ctime_is_not_just_mtime`, `inode/directory_*`).
- **Limitation:** u9fs stores a tab in a name as `\09`, as its manual page documents
  (`namespace/hostile_names`).

### diod (9P2000.L)

- **Likely defect (Linux 9p client, `cache=loose` and `cache=mmap`):** four processes appending to one file
  with `O_APPEND` on one mount lose bytes `write()` reported (`shared.bin`). With `cache=none` nothing is
  lost. [`9p-append.sh diod loose`](../scripts/showcase/repro/9p-append.sh): 3276800 bytes reported written,
  897024 in the file. With `cache=mmap`, `observed.log` also loses blocks in some runs.
- **Likely defect (Linux 9p client with `cache=loose`; whether diod contributes is not established):** a file
  read before its directory is renamed is listed under the new name, and opening it there fails with ENOENT
  (`malice/dir_rename_open`, `namespace/alphabet_path`, `namespace/deep_rename_above`,
  `namespace/populated_subtree_moves`, `namespace/rename_into_own_subtree`,
  `inode/subtree_move_keeps_inodes`).
  [`9p-rename-dir-loose.sh`](../scripts/showcase/repro/9p-rename-dir-loose.sh):

  ```sh
  mkdir d && echo hello > d/f && cat d/f && mv d d2
  ls d2        # f
  cat d2/f     # No such file or directory
  ```

- **Limitation:** diod implements POSIX locks with whole-file `flock()` (`diod_ops.c`: "Implement POSIX locks
  in terms of BSD flock locks"), so byte ranges, shared locks and `F_GETLK` do not behave as POSIX describes
  (`locks/*`).
- **Not established (`cache=none`):** in `shared.bin`, blocks are overwritten by other appends, with no
  bytes lost.
