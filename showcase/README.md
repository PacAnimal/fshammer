# Showcase

fshammer run against filesystems that can be set up on a GitHub Actions runner (`ubuntu-24.04`, kernel
`6.17.0-1022-azure`, Python 3.12), with a standalone reproducer for each behaviour this page calls a likely
defect and for some of those it leaves open.

Where it was practical, the software under test is built from its latest upstream release as of 2026-10-18,
because these results are meant to be reported upstream: davfs2 1.7.3, sshfs 3.7.6, bindfs 1.18.4, libfuse
3.18.3 (for sshfs and bindfs), diod 1.1.0, NFS-Ganesha V15.9 (NFS; tagged 2026-10-16, while GitHub's release list still shows V15.8) and
rclone v1.75.2. NFS-Ganesha 15.9 removed
its 9P server, so the 9P targets use 15.8, the last release that has one. Samba 4.19.5,
Apache 2.4.58 and the NFS server userspace (nfs-utils 2.6.4) are Ubuntu 24.04's packages, behind their current
releases (Samba 4.25.0, Apache 2.4.69, nfs-utils 3.1.1). knfsd and the 9p, SMB, NFS and FUSE clients are the runner's kernel,
which is Ubuntu's Azure build of 6.17, not mainline. u9fs has no releases; it is built from the last commit of
its archive. Before reporting anything here, reproduce it on the component's current release.

## What the workflow checks

The [Showcase workflow](https://github.com/PacAnimal/fshammer/blob/main/.github/workflows/showcase.yml) runs
on every push to `main` that changes `fshammer.py`, `scripts/showcase/` or the workflow itself, weekly, and on
demand. A newer run cancels an older one. GitHub disables scheduled workflows in a repository with no activity
for 60 days.

- **A target job** runs fshammer once (`--duration 0 --procs 4 --seed 1`, plus `--alt-dir` for targets mounted
  twice and `--server-root` where the server writes before acknowledging; see
  [`run.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/run.sh)) and compares its distinct failing
  checks with [`scripts/showcase/expected/<target>.txt`](https://github.com/PacAnimal/fshammer/tree/main/scripts/showcase/expected).
  A failing check is a `FAIL` line reduced to what precedes its first `: `, with a path reduced to its file
  name and digits replaced by `N`, so one check that fails for many files counts once; a writer's own files
  (`wN-N-<kind>.bin`) all count as one check, `FAIL own files:`. The job fails if its
  exit code does not match whether it printed `FAIL` lines, a listed check is missing, an unlisted one
  appears, or fshammer does not finish within the target's timeout, unless the file lists `TIMEOUT` (it must
  not finish) or `? TIMEOUT` (it may not). Checks marked `?` failed in some recorded runs and not others; they
  are allowed but not required. Each listed check is tagged `[defect]`, `[follows]` (fails because of another
  failure of the same target), `[open]` or `[limitation]`, matching the labels on this page.
- **A reproducer job** runs a script from [`scripts/showcase/repro/`](https://github.com/PacAnimal/fshammer/tree/main/scripts/showcase/repro)
  that sets up the target and makes a few system calls, without fshammer.
  [`repros.txt`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repros.txt) states whether
  each is expected to reproduce; the job fails when the outcome differs.
- **The status job** writes a [shields.io](https://shields.io/badges/endpoint-badge) badge file per target, and
  a `summary.json`, to the `status` branch after each run on `main` in which no job was cancelled, unless a
  newer push or scheduled showcase run on `main` exists that was not cancelled. A run started by hand publishes
  only when it runs every target in `assert` mode. A badge describes that run:
  - green `no failures`: fshammer exited 0 and printed no `FAIL` line;
  - yellow `N failing checks (documented)`: every failing check is tagged `[limitation]`;
  - orange `N failing checks`: at least one failing check is tagged `[defect]`, `[follows]` or `[open]`;
  - orange `does not finish`: fshammer did not finish, and the record says it does not, or may not;
  - red `changed`, `reproducer changed` or `setup failed`: the outcome differs from the record (a hang that
    was not recorded included), a reproducer's outcome differs, or the target could not be set up.

  `N` counts distinct failing checks in that run, optional ones included.
- Running the workflow by hand with `mode: record` runs each target three times and writes a proposed
  expectations file instead of asserting.

## Running a target yourself

```sh
scripts/showcase/run.sh samba                          # one target
scripts/showcase/repros.py run samba-hardlink /tmp/r   # one reproducer
```

Only in a throwaway VM or container, with sudo. The setup scripts:

- install Ubuntu packages (among them nfs-kernel-server, nfs-common, rpcbind, samba, cifs-utils, apache2,
  openssh-server, fuse3, build-essential and git), and build and install davfs2, sshfs, bindfs, libfuse and
  diod under `/usr/local` (downloaded from their release pages and checked against a SHA-256), NFS-Ganesha
  from a `git clone` of its release tag (checked against the tag's commit), u9fs from a `git clone`, and
  rclone from a `.deb` from downloads.rclone.org (checked against the SHA-256 rclone publishes);
- overwrite `/etc/exports` and `/etc/samba/smb.conf`, write `/etc/ganesha/showcase.conf` and an Apache
  configuration, enable Apache's `dav` and `dav_fs` modules, and append to `/etc/davfs2/secrets`;
- add a system user `davfs2`; set a Samba password (`showcase`) for the current user; create `/var/www/dav`;
- for sshfs, generate `~/.ssh/showcase`, a key without a passphrase, and add it to `~/.ssh/authorized_keys`;
- stop the kernel NFS server where it was started by its package, and set its NFSv4 grace and lease times and
  the NLM grace period to 10 seconds;
- start rpcbind, rpc.statd, rpc.mountd, nfsd, smbd, sshd, NFS-Ganesha, u9fs and diod, and restart smbd and
  Apache. u9fs (`-a none`) and diod (`--no-auth`) run as root without authentication;
- for the two-client targets, create a network namespace `fshammer-client2` and a veth pair (`fshv0`, with
  10.231.0.1/24 on the host side);
- install a setuid-root `fusermount3` (from libfuse 3.18.3) in `/usr/local/bin`, ahead of `/usr/bin` in the
  usual `PATH`; install davfs2 with `--sysconfdir=/etc`, which writes `/etc/davfs2`; install Ubuntu's diod
  package too, for the diod 1.0.24 control;
- start servers, and leave everything mounted and running. Nothing is cleaned up.

The servers are configured to work, not to be secure: most listen on every address, and the NFS exports, the
Apache `/dav` share, u9fs and diod accept any client. u9fs and diod listen on 127.0.0.1.

tmpfs, ext4, XFS, Btrfs and overlayfs are mounted once and bind-mounted a second time, so both mounts are
one filesystem and fshammer reports that as a gap; ext4, XFS and Btrfs are on sparse loop images. overlayfs has
an empty lower directory, so files are never copied up. Samba, knfsd, NFS-Ganesha and the 9p servers export a loop-mounted ext4 image. sshfs, bindfs,
rclone and Apache serve a directory on the runner's own filesystem. None of these runs fills a filesystem or
uses `--crash-command`, so ENOSPC handling and durability across a crash are not covered here.

## Results

<!-- showcase-table -->
| Target | Version | Distinct failing checks | Reproducers | Controls |
|---|---|---|---|---|
| tmpfs, bind-mounted twice (control) | kernel | 0 |  |  |
| ext4 on a loop image, bind-mounted twice | kernel | 0 |  |  |
| XFS on a loop image, bind-mounted twice | kernel | 0 |  |  |
| Btrfs on a loop image, bind-mounted twice | kernel | 0 |  |  |
| overlayfs over ext4, bind-mounted twice | kernel | 0 |  |  |
| [knfsd, NFSv3, mounted twice on one client](#knfsd) | kernel | 0, 1 optional |  | 2 |
| [knfsd, NFSv4.2, mounted twice on one client](#knfsd) | kernel | 0, 2 optional | 1 | 1 |
| [knfsd, NFSv4.2, two clients](#knfsd) | kernel | 0, 1 optional |  | 1 |
| [NFS-Ganesha, NFSv3, mounted twice on one client](#nfs-ganesha) | 15.9 | 0, 1 optional |  | 1 |
| [NFS-Ganesha, NFSv4.0, mounted twice on one client](#nfs-ganesha) | 15.9 | 0 |  |  |
| [NFS-Ganesha, NFSv4.1, mounted twice on one client](#nfs-ganesha) | 15.9 | 1, 1 optional | 1 |  |
| [NFS-Ganesha, NFSv4.2, mounted twice on one client](#nfs-ganesha) | 15.9 | 1, 1 optional | 1 | 1 |
| [NFS-Ganesha, NFSv4.2, two clients](#nfs-ganesha) | 15.9 | 1, 1 optional |  | 1 |
| [Samba, Linux SMB3 client, mounted twice](#samba) | 4.19.5 (Ubuntu) | 12, 1 optional | 2 |  |
| [davfs2 over Apache mod_dav](#davfs2) | 1.7.3 | 4, 2 optional | 1 |  |
| [davfs2, Ubuntu's package](#davfs2-170-ubuntu-2404) | 1.7.0 (Ubuntu) | 26, 1 optional | 1 |  |
| [sshfs, mounted twice](#sshfs) | 3.7.6, libfuse 3.18.3 | 19 |  |  |
| [bindfs, mounted once](#bindfs) | 1.18.4, libfuse 3.18.3 | 0 |  |  |
| [bindfs, mounted twice](#bindfs) | 1.18.4, libfuse 3.18.3 | 2 |  |  |
| [rclone mount, `--vfs-cache-mode writes`](#rclone) | v1.75.2 | 13 | 2 |  |
| [9p: u9fs, `cache=none`](#u9fs-9p2000) | `fcec3e1` | 13, 1 optional | 4 |  |
| [9p: u9fs, `cache=mmap`](#u9fs-9p2000) | `fcec3e1` | 14, 1 optional; may not finish | 2 |  |
| [9p: diod, `cache=none`](#diod-9p2000l) | 1.1.0 | 6, 3 optional | 1 | 1 |
| [9p: diod, `cache=loose`](#diod-9p2000l) | 1.1.0 | 7, 3 optional | 2 | 1 |
| [9p: diod, `cache=mmap`](#diod-9p2000l) | 1.1.0 | 6, 4 optional | 3 |  |
| [9p: NFS-Ganesha, `cache=none`](#nfs-ganesha-9p2000l) | 15.8 (9P removed in 15.9) | 8 | 1 |  |
| [9p: NFS-Ganesha, `cache=loose`](#nfs-ganesha-9p2000l) | 15.8 (9P removed in 15.9) | 8 |  |  |
| [9p: NFS-Ganesha, `cache=mmap`](#nfs-ganesha-9p2000l) | 15.8 (9P removed in 15.9) | 8 |  |  |
<!-- /showcase-table -->

Each failure below is labelled:

- **Likely defect (component)**: reproduced without fshammer on the version listed, with evidence pointing at
  that component. Reproduce it on the current release before reporting.
- **Fixed upstream**: reproduced on the version listed, which is a distribution's package, and fixed in a later
  release of the component.
- **Limitation**: documented, or inherent in the protocol; the source is given.
- **Not established**: observed, with no conclusion about which component is responsible or whether it is a
  defect. The next step to find out is given where there is one.

A failure caused by another one in the same section is noted as such.

## knfsd

The Linux kernel NFS server exporting an ext4 directory, mounted twice. In the "one client" targets both mounts
are on this host over loopback, the second with `nosharecache`: they have separate caches but share one NFS
client (one set of sockets and lockd; with NFSv4, one client ID, under which the server grants delegations, which the client tracks per mount), so the server
sees a single client. In the
"two clients" target the second mount is made from another network namespace, through a veth pair, under its
own hostname ([`netns.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/netns.sh)), so the
server sees two clients.

```sh
echo "/srv/export 127.0.0.1(rw,sync,no_subtree_check,no_root_squash,fsid=0)" > /etc/exports
exportfs -ra && systemctl start nfs-server
mount -t nfs -o vers=4.2 127.0.0.1:/ /mnt/a
mount -t nfs -o vers=4.2,nosharecache 127.0.0.1:/ /mnt/b
```

- **Not established (the evidence points at the Linux NFS client):** with NFSv4.2 and both mounts on one client, while one process
  holds an exclusive `fcntl` lock through `/mnt/a`, another process is granted the same lock through `/mnt/b`.
  [`nfs-lock-two-mounts.sh knfsd 4.2 same`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/nfs-lock-two-mounts.sh)
  reproduces it in every run; fshammer's `locks/cross_mount_excludes` catches it only in some runs. With two
  clients (`nfs-lock-two-mounts.sh knfsd 4.2 2client`) the second lock is refused, and with NFSv3 it does not
  happen. That points at the client deciding the lock locally, for example under a delegation, separately for
  each of its two superblocks. Repeating the one-client case with delegations disabled on the server
  (`fs.leases-enable=0`) and with a packet capture would settle it.
- **Limitation:** `shared.bin` sometimes loses appends made through both mounts, or through two clients
  (optional). `open(2)`:
  "O_APPEND may lead to corrupted files on NFS filesystems if more than one process appends data to a file at
  once. This is because NFS does not support appending to a file, so the client kernel has to simulate it,
  which can't be done without a race condition." fshammer still reports it, tagged as documented, because the
  check is the same on every filesystem.

## NFS-Ganesha

NFS-Ganesha 15.9, built from its release with the VFS FSAL, exporting an ext4 directory, mounted twice with
NFSv3, 4.0, 4.1 and 4.2 on one client as for knfsd, and with NFSv4.2 from two clients. The configuration, from
[`ganesha.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/ganesha.sh):

```
NFS_CORE_PARAM { Protocols = 3, 4; Plugins_Dir = /usr/local/lib/ganesha; }
NFSV4 { Grace_Period = 10; Lease_Lifetime = 10; Allow_Numeric_Owners = true; Only_Numeric_Owners = true; }
EXPORT {
    Export_Id = 1; Path = /srv/export; Pseudo = /export; Squash = No_Root_Squash; Protocols = 3, 4;
    FSAL { Name = VFS; }
    Access_Type = RW;
}
```

- **Not established (NFS-Ganesha 15.9 or the Linux NFS client):** with NFSv4.1 and 4.2, when four processes
  on one mount take turns at an exclusive `lockf(F_LOCK)` on one file, every lock call after the first grant
  fails with EIO; one process taking and releasing the same lock repeatedly has no errors.
  [`nfs-lock-contention.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/nfs-lock-contention.sh),
  one process: 16 lock calls, none failed; four processes: 64 lock calls, 63 failed with EIO, and the counter
  they increment reads 1.
  fshammer's `locks/exclusive_rmw` hits it in every recorded run. It does not happen with knfsd. A packet capture
  would show which reply to the blocked LOCK the client reports as EIO.
- Unlike knfsd, a lock held through one mount excludes the same lock through the other, with one client and
  with two.
- **Limitation:** `shared.bin` sometimes loses appends made through two mounts or two clients (optional), as for
  knfsd.
- NFSv3 runs with Ganesha in the second network namespace, with its own rpcbind and statd, reached at
  10.231.0.2. With Ganesha and the client on one rpcbind, the first `fcntl` lock never returned: the kernel's
  lockd, which the client side starts, and Ganesha's NLM service both register NLM (RPC program 100021) with
  that rpcbind, so the client's NLM call could reach the kernel's lockd instead of Ganesha. That was the
  setup, not Ganesha:
  [`nfs-v3-lock.sh ganesha`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/nfs-v3-lock.sh),
  which takes one uncontended lock, is now a control that must return.
- NFSv4.0 shows no failures; NFSv3 shows only the `shared.bin` limitation above, in some runs.

## Samba

Samba serving an ext4 directory, mounted twice with the Linux SMB3 client, the second mount with `nosharesock`
(a separate session). Ubuntu's Samba 4.19 does not serve the SMB3 POSIX extensions, so this is plain SMB3.

```sh
mount -t cifs //127.0.0.1/share /mnt/a -o vers=3.1.1,username=$USER,password=PASSWORD,uid=$(id -u),gid=$(id -g)
mount -t cifs //127.0.0.1/share /mnt/b -o vers=3.1.1,username=$USER,password=PASSWORD,uid=$(id -u),gid=$(id -g),nosharesock
```

- **Not established (Samba 4.19.5 or the Linux SMB3 client):** after `link(a, b)` both names report
  `st_nlink` 2, and opening `b` fails with EINVAL (`namespace/hardlink`, and `malice/negatives` in runs where
  that variant makes a link).
  [`samba-hardlink.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/samba-hardlink.sh):

  ```python
  open("/mnt/a/x", "wb").write(b"hello")
  os.link("/mnt/a/x", "/mnt/a/y")
  open("/mnt/a/y", "rb")            # OSError: [Errno 22] Invalid argument
  ```

- **Not established (Samba 4.19.5 or the Linux SMB3 client):** an mtime set with `utime()` while written data
  is still buffered is replaced by the time of the `close()` (`identity/utimes_survives_the_flush`).
  [`samba-utime-before-close.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/samba-utime-before-close.sh):

  ```python
  fd = os.open("/mnt/a/t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
  os.write(fd, os.urandom(40960))
  os.utime("/mnt/a/t", (time.time() - 86400,) * 2)
  os.close(fd)
  os.stat("/mnt/a/t").st_mtime      # the time of the close, not a day ago
  ```

  For both, a capture of the SMB traffic, or the same steps against Samba 4.25 and against a Windows server,
  would tell client and server apart.
- **Limitation:** without the POSIX extensions the client reports modes from its `file_mode` and `dir_mode`
  mount options (mount.cifs(8)), so `chmod` does not stick and mode 0000 is not enforced
  (`identity/chmod_keeps_bytes_and_mtime`, `malice/mode_is_enforced`).
- **Limitation:** an SMB lock belongs to the open handle it was taken on ([MS-SMB2], "Receiving an SMB2 LOCK
  Request"), not to a process, so closing another descriptor does not drop it (`locks/close_drops_every_lock`).
- **Limitation:** an unlock must name exactly the offset and length of a held lock, or it fails with
  STATUS_RANGE_NOT_LOCKED ([MS-FSA], "Server Requests an Unlock of a Byte-Range"), so a range cannot be split
  (`locks/unlock_splits_a_range`).
- **Limitation:** a name cannot contain a backslash, which is SMB's path separator ([MS-FSCC] 2.1.5)
  (`namespace/hostile_names`).
- **Limitation:** a directory with a file open beneath it, through the same connection, cannot be renamed
  (EACCES): Samba refuses it with NT_STATUS_ACCESS_DENIED unless the rename is a POSIX one. With the default
  `strict rename = no`, which the showcase uses, it checks the files open in the same smbd process
  (`file_find_subpath()` in `source3/smbd/smb2_reply.c`); with `strict rename = yes` it checks every process
  (`have_file_open_below()`) (`malice/dir_rename_open`, `namespace/deep_rename_above`).
- **Not established:** directories report `st_nlink` 2 and size 0 whatever they hold
  (`inode/directory_link_count_is_usable`, `inode/directory_size_is_usable`); in `shared.bin`, appended through
  the two sessions, appends are lost or overwrite each other ("block frame broken").

## davfs2

davfs2 1.7.3, built from its release, mounting Apache mod_dav on the same host.

```sh
# Apache: Alias /dav /var/www/dav, and "Dav On" for that directory
echo '/mnt/dav "" ""' >> /etc/davfs2/secrets
/usr/local/sbin/mount.davfs http://127.0.0.1/dav /mnt/dav -o uid=$(id -u),gid=$(id -g)
```

- **Likely defect (davfs2 1.7.3):** a file written, given an mtime with `utime()`, and closed reads back empty
  straight after `close()` (`identity/utimes_survives_the_flush`); read again 15 seconds later it has its
  4096 bytes. The empty read is answered without a request to the server: in Apache's access log for the
  file, the only request before it is a `LOCK`; the `PUT` follows 11 seconds later (davfs2 delays uploads by
  `delay_upload`, 10 seconds by default), and the only `GET` is the later re-read. The reproducer prints that log.
  [`davfs2-utime-before-close.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/davfs2-utime-before-close.sh):

  ```python
  fd = os.open("/mnt/dav/u", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
  os.write(fd, os.urandom(4096))
  os.utime("/mnt/dav/u", (time.time() - 86400,) * 2)
  os.close(fd)
  open("/mnt/dav/u", "rb").read()   # b''
  ```

- **Not established:** file names longer than 248 bytes, and directory names longer than 244, fail with EIO
  rather than being stored or refused with ENAMETOOLONG (`namespace/hostile_names`, `namespace/long_names`).
  davfs2 names its local cache file after the entry, as `<name>-XXXXXX` for a file and `dir-<name>-XXXXXX`
  for a directory (`src/cache.c`), which leaves 248 and 244 bytes of the 255 a name may have.
- **Not established:** `fstat()` on an open file that has been unlinked fails with ENOENT
  (`malice/unlink_open`).
- **Not established:** in some runs, creating a chain of 26 nested one-letter directories with `makedirs()`
  fails partway with ENOENT, a directory just created not being found (`namespace/alphabet_path`; also seen
  as `malice/log_rotation`, where `open(O_CREAT)` in a just-created directory fails with ENOENT).

Symlinks (ENOSYS), hard links (EPERM) and extended attributes (ENOTSUP) are refused with errors fshammer
accepts, so those variants are reported as gaps.

### davfs2 1.7.0 (Ubuntu 24.04)

Ubuntu 24.04's package, otherwise set up the same way.

- **Fixed upstream:** every directory listing fails with EINVAL on this kernel; reading files works. davfs2
  1.7.0 answers EINVAL to a FUSE read larger than its buffer (`dav_fuse.c`), and since Linux 6.16 the kernel
  asks for larger directory reads (commit dabb9039, "fuse: increase readdir buffer size"). davfs2 1.7.2 fixed
  it ("Increase buf read size to 64 (fixes read issue on linux 6.16)"); in 1.7.0, `buf_size 64` in
  davfs2.conf(5) works around it.
  [`davfs2-readdir.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/davfs2-readdir.sh).
  The other failures of this target follow from it (`namespace/hostile_names` and `namespace/long_names`
  fail here on the listing, not on the name length), except `identity/utimes_survives_the_flush` and
  `malice/unlink_open`, which 1.7.3 shows too (above); on 1.7.0 they are not reproduced separately.

## sshfs

sshfs 3.7.6 built against libfuse 3.18.3, mounted twice from an sshd on the same host.

```sh
sshfs $USER@127.0.0.1:/srv/export /mnt/a
sshfs $USER@127.0.0.1:/srv/export /mnt/b
```

- **Limitation:** `symlink()` creates a link with an absolute target, or one containing `..`, and `readlink()`
  on it then fails with EPERM (`namespace/symlink_*`, `namespace/populated_subtree_moves`,
  `namespace/rename_into_own_subtree`). sshfs 3.7.6 rejects such targets by default; its ChangeLog lists
  `-o contain_symlinks` and `-o no_contain_symlinks` with the fix for CVE-2026-47187, and `sshfs --help`
  describes `contain_symlinks` as "reject absolute symlinks and symlinks containing ..". sshfs 3.7.3 has no
  such option and showed none of these failures.
- **Limitation:** inode numbers come from libfuse, not from the server: SFTP version 3 carries none, and sshfs
  enables neither of libfuse's `use_ino` and `readdir_ino` options. So a listing reports inode 4294967295
  (`FUSE_UNKNOWN_INO`) where `stat()` reports libfuse's own number, and a hard link's two names have two
  numbers (`inode/*`, `walk/walk_manifest`, `namespace/hardlink`). fuse.h documents
  both options; with `-o readdir_ino` the listing and `stat()` agree.
- **Limitation:** SFTP version 3 has no ctime, so ctime equals mtime (`identity/ctime_is_not_just_mtime`).
- **Limitation:** sshfs does not implement locking, so locks are kept by the kernel per mount and a lock
  through one mount does not exclude the other (`locks/cross_mount_excludes`). fuse.h documents that without
  a lock method "the kernel will still allow file locking to work locally".
- **Not established:** `utime(path, (0, 0))` leaves the mtime at the current time (`identity/sentinel_mtimes`);
  a 256-byte name fails with EBADMSG rather than ENAMETOOLONG (`namespace/hostile_names`); in `shared.bin`,
  appended through both mounts, blocks are overwritten by appends made through the other mount.

## bindfs

bindfs 1.18.4 built against libfuse 3.18.3, of a local directory, mounted once and twice.

```sh
bindfs --no-allow-other /srv/src /mnt/a
bindfs --no-allow-other /srv/src /mnt/b
```

Mounted once, it passes.

- **Limitation (two mounts):** in bindfs's default configuration locks are kept by the kernel per mount, so a
  lock through one mount does not exclude the other (`locks/cross_mount_excludes`). bindfs(1):
  `--enable-lock-forwarding` forwards locks to the source directory, needs `--multithreaded`, and is off by
  default.
- **Not established (two mounts):** as for sshfs, blocks in `shared.bin` are overwritten by appends made
  through the other mount.

## rclone

`rclone mount` v1.75.2 of a local directory with `--vfs-cache-mode writes` (without a cache mode rclone can
only write files sequentially) and `--links` (symlinks, off by default).

```sh
rclone mount /srv/remote /mnt/r --vfs-cache-mode writes --links --cache-dir /srv/cache --daemon
```

- **Likely defect (rclone v1.75.2):** extending a file with `truncate()` fails with EIO
  (`identity/truncate_grow_reads_zeros`). The remote is a local directory, where the same call works, so the
  EIO is rclone's VFS layer's answer.
  [`rclone-truncate-grow.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/rclone-truncate-grow.sh):

  ```python
  open("/mnt/r/g", "wb").write(b"x" * 100)
  os.truncate("/mnt/r/g", 4096)     # OSError: [Errno 5] Input/output error
  ```

- **Likely defect (rclone v1.75.2):** a rename over a file that is held open with `O_APPEND` does not leave
  the renamed file intact (`malice/rename_over_open`). fshammer has seen two outcomes: in some runs the name is
  missing (FileNotFoundError) when it is read afterwards; in others a write made afterwards through the held
  descriptor lands in the file that replaced it ("holds 8192 bytes, expected the source's 4096"). The
  reproducer shows the second. The remote is a local directory, where a rename over an open file keeps
  both files intact, so where the write lands is decided in rclone's VFS layer.
  [`rclone-rename-over-open.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/rclone-rename-over-open.sh):

  ```python
  open("/mnt/r/dst", "wb").write(b"V" * 4096)
  open("/mnt/r/src", "wb").write(b"S" * 4096)
  held = os.open("/mnt/r/dst", os.O_WRONLY | os.O_APPEND)
  os.rename("/mnt/r/src", "/mnt/r/dst")
  os.write(held, b"G"); os.close(held)
  len(open("/mnt/r/dst", "rb").read())   # 4097, or the name is missing
  ```

- **Limitation:** rclone does not store permissions; it reports `--file-perms` (default 0666) narrowed by
  the umask (rclone mount documentation), so `chmod` does not stick and modes are not enforced
  (`identity/chmod_keeps_bytes_and_mtime`, `malice/mode_is_enforced`, `namespace/symlink_into_mount`).
- **Not established:** ctime equals mtime (`identity/ctime_is_not_just_mtime`); a rename gives the file a new
  inode number (`inode/rename_keeps_inode` and four other `inode/*` checks); directories report size 0
  (`inode/directory_size_is_usable`); a 256-byte name fails with EIO rather than ENAMETOOLONG
  (`namespace/hostile_names`).

## 9p

The Linux 9p client over TCP to three servers, each exporting an ext4 directory. The client requests a 1 MiB
`msize`; u9fs and diod accept it, NFS-Ganesha offers 65536.

### u9fs (9P2000)

u9fs is Plan 9's Unix 9P server, built from [github.com/Plan9-Archive/u9fs](https://github.com/Plan9-Archive/u9fs)
at `fcec3e1` (2021), an archive with no maintainer to report to. It is here for the behaviour of the Linux
client with a 9P2000 server.

```sh
# as root, one process per connection, as inetd would run it; u9fs switches to the attaching user
socat TCP-LISTEN:5640,bind=127.0.0.1,reuseaddr,fork "EXEC:u9fs -a none -m 1048576 /srv/export" &
mount -t 9p -o trans=tcp,port=5640,aname=/srv/export,version=9p2000,uname=$USER,access=user,msize=1048576,cache=none,dfltuid=$(id -u),dfltgid=$(id -g) 127.0.0.1 /mnt/9
```

- **Likely defect (Linux 9p client):** two processes on one mount are both granted an exclusive `fcntl` lock
  on the same range (`locks/close_drops_every_lock`, `conflict_excludes`, `exclusive_rmw`,
  `getlk_names_the_holder`, `shared_is_shared`, `unlock_splits_a_range`). 9P2000 has no lock messages, and for
  it the client's `v9fs_file_lock`
  (`fs/9p/vfs_file.c`) returns success without recording the lock. Reproduced on Ubuntu's 6.17.0-1022-azure
  kernel only; the cited code is the same in mainline v7.2.
  [`9p-locks-9p2000.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-locks-9p2000.sh).
- **Not established (Linux 9p client or u9fs, `cache=mmap`):** a file appended to while another process calls
  `stat()` on it loses bytes `write()` reported (`observed.log`). With `cache=mmap`,
  `v9fs_vfs_getattr` starts writeback without waiting for it and then replaces the file size with the
  server's (`v9fs_stat2inode` without `V9FS_STAT2INODE_KEEP_ISIZE`), while written pages are still on their
  way; later appends start from the smaller size.
  [`9p-stat-during-append.sh u9fs`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-stat-during-append.sh):
  400 writes of 32 KiB, 13107200 bytes reported written; how much is missing varies between runs. The same
  reproducer against diod, which goes through the same client sequence (`v9fs_vfs_getattr_dotl`), loses bytes
  in some runs and none in others (32768 bytes in one run), so it is not specific to u9fs, though u9fs loses
  far more; which component is at fault is not established.
  Reproduced on 6.17.0-1022-azure only; the cited code is the same in mainline v7.2.
- **Not established (Linux 9p client):** four processes appending to one file with `O_APPEND` on one mount,
  with `cache=none`, lose bytes. Each write goes through `netfs_unbuffered_write_iter()`
  (`fs/netfs/direct_write.c`), which takes the inode lock shared (`netfs_start_io_direct()`) and then lets
  `generic_write_checks()` set an `O_APPEND` write's position to the cached file size; two appenders holding
  the shared lock at once can be given the same position. 9P2000's open modes have no append flag (it has
  only the `DMAPPEND` file attribute), so the server cannot place the write; with 9P2000.L the client opens
  with `P9_DOTL_APPEND`, which is why diod with `cache=none` loses nothing.
  [`9p-append.sh u9fs none`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-append.sh).
  fshammer's `shared.bin` loses appends in some runs, with `cache=none` and `cache=mmap` alike.
- **Not established (u9fs):** renaming any entry directly under the directory u9fs serves fails, and the same
  rename one level down works. u9fs answers the rename with the error string "whoops: can't happen in u9fs"
  (`u9fs.c`, wstat). fshammer works one level down and never hits this.
  [`u9fs-rename-at-root.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/u9fs-rename-at-root.sh).
  The client has no errno for that string, so `net/9p/error.c` maps it to ESERVERFAULT (526), which is defined
  in the kernel's internal `include/linux/errno.h` ("An untranslatable error occurred"), and the program sees
  "Unknown error 526".
- **Not established (u9fs):** `fstat()` on an open file that has been unlinked fails with ENOENT
  (`malice/unlink_open`), and so does `fsync()` on a file whose ancestor directory was renamed
  (`namespace/deep_rename_above`). u9fs answers a stat by looking up the file's path (`u9fs.c`, `fidstat`);
  diod 1.1.0 answers both.
  [`9p-unlink-open.sh u9fs`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-unlink-open.sh).
- **Not established (Linux 9p client):** a shared writable mapping is refused (EINVAL) in both cache modes
  (`sqlite/mmap_write_through`). For 9P2000 the client maps files read-only in every cache mode, while
  `Documentation/filesystems/9p.rst` describes `cache=mmap` as "read-ahead + writeback file cache".
- **Limitation:** 9P2000's stat has no ctime and no link count, so ctime equals mtime and directories report
  `st_nlink` 2 (`identity/ctime_is_not_just_mtime`, `inode/directory_link_count_is_usable`). u9fs reports
  directories with length 0 (`u9fs.c`, `fidstat`) (`inode/directory_size_is_usable`).
- **Limitation:** u9fs stores a tab in a name as `\09`, as its manual page documents
  (`namespace/hostile_names`).
- **Not established (`cache=mmap`):** in some runs fshammer does not finish within 30 minutes. In the one
  captured, two writers were in uninterruptible sleep in a `write()`: one in `folio_wait_writeback`, called
  from `netfs_perform_write`, waiting for a page's writeback that did not complete; the other in
  `netfs_start_io_write`, waiting for the first. Whether u9fs stopped answering or the client lost the reply is
  not established; the next step is a capture of the 9P traffic. The job saves each process's state, kernel
  wait channel and stack to `hang.txt` in its artifact.

### diod (9P2000.L)

diod 1.1.0, built from its release.

```sh
diod --no-auth --export /srv/export --listen 127.0.0.1:564 &
mount -t 9p -o trans=tcp,port=564,aname=/srv/export,version=9p2000.L,uname=$USER,access=user,msize=1048576,cache=loose,dfltuid=$(id -u),dfltgid=$(id -g) 127.0.0.1 /mnt/9
```

- **Likely defect (diod 1.1.0):** after a file held open is renamed, the server keeps answering for its old
  path. What fshammer reports:
  - `malice/rename_live` (`cache=none` and `cache=mmap`): `fstat()` on the open descriptor fails with ENOENT;
  - `malice/log_rotation` (`cache=loose`): reading the renamed file fails with ENODATA;
  - `malice/rename_recreate` (`cache=loose`): "the renamed file did not receive its own handle's writes", or
    reading it fails with ENODATA;
  - `inode/three_rename_swap` (`cache=loose`): "the swap moved the names and left the bytes behind".

  diod 1.1.0 implements Trenameat (its NEWS: "Implement Trenameat and Tunlinkat"), and `diod_renameat`
  (`src/libdiod/diod_ops.c`) renames on disk without updating the path of any fid that refers to the file,
  where `diod_rename` (Trename) updates `f->path`. A fid that is not open answers getattr with `lstat()` of
  that path. diod 1.0.24 has no Trenameat, so the client falls back to Trename, and the same steps work there.
  [`9p-rename-open.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-rename-open.sh)
  (run for `none`, `loose` and `mmap`, and against diod 1.0.24 as a control; with `none` and `mmap` it shows
  the `fstat()` failure, and the renamed file has all its bytes):

  ```python
  fd = os.open("/mnt/9/service.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
  os.write(fd, b"A" * 100)
  os.rename("/mnt/9/service.log", "/mnt/9/service.log.1")
  os.fstat(fd)                      # cache=none: FileNotFoundError, [Errno 2]
  ```

- **Not established (`cache=none` and `cache=mmap`):** fshammer's `malice/log_rotation` finds writes made
  through the descriptor after the rotation missing from the renamed file. The reproducer, which makes the same
  rename, finds the renamed file complete; it may be the same fault reached differently.
- **Not established (the Linux 9p client's 9P2000.L path, or diod 1.1.0; `cache=loose` and `cache=mmap`):**
  four processes appending to one file with `O_APPEND` on one mount lose bytes `write()` reported
  (`shared.bin`). With a page cache the client chooses the offsets it writes at, and with `cache=none` diod
  receives the same appends and nothing is lost, which points at the client; it has not been compared with
  another 9P2000.L server, so diod is not ruled out.
  [`9p-append.sh diod loose`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/9p-append.sh).
- **Not established (`cache=mmap`):** in some runs `observed.log`, appended to while other processes call
  `stat()` on it, loses or tears blocks. `v9fs_vfs_getattr_dotl` has the same sequence as the 9P2000 path
  described for u9fs above; `9p-stat-during-append.sh` loses bytes with diod in some runs and none in others.
- **Limitation:** diod implements POSIX locks with whole-file `flock()` (`diod_ops.c`: "Implement POSIX locks
  in terms of BSD flock locks"), so byte ranges, shared locks and `F_GETLK` do not behave as POSIX describes
  (`locks/*`). Which of the lock checks fail varies between runs.
- **Limitation (`cache=none`):** a shared writable mapping is refused (`sqlite/mmap_write_through`);
  `Documentation/filesystems/9p.rst` gives `cache=none` as "all caches disabled, mmap disabled".

### NFS-Ganesha (9P2000.L)

NFS-Ganesha 15.8's 9P server, built and configured as above but serving 9P instead of NFS, mounted once with
the same client options as diod. NFS-Ganesha 15.9 removed the 9P server ("Removal of 9P code"), so there is no
current release to report these results against; they are kept for the behaviour of the Linux 9p client with a
second 9P2000.L server.

```sh
mount -t 9p -o trans=tcp,port=564,aname=/srv/export,version=9p2000.L,uname=$USER,access=user,msize=1048576,cache=loose,dfltuid=$(id -u),dfltgid=$(id -g) 127.0.0.1 /mnt/9
```

- **Not established (NFS-Ganesha 15.8's 9P server or the Linux 9p client):** data written through the mount
  cannot be written back or read back. In fshammer a write descriptor's `fsync()` or `close()` fails with
  EIO ("writes were still not on the filesystem at close") and, once the connection has been reset, with
  ECONNRESET, and reading the written files back fails with EIO
  ("own file could not be read back"), so the other phases have nothing to work on; every failing check here
  follows from this. In the reproducer, which creates one file with `open()`, the create itself fails with
  EFAULT ("Bad address") and listing the directory then fails with EIO.
  The reproducer turns on Ganesha's debug log and prints what it logged: the `TLCREATE` arrives and Ganesha
  opens the new file ("Opened fd=N for file created-…"), so the request reaches the server and the failure is
  in what comes back. It also prints the negotiated `msize`, 65536, and that `mkdir` as root fails with EIO. A capture of the 9P
  traffic would show whether its replies are malformed or the client misreads them.
  [`ganesha-9p-create.sh`](https://github.com/PacAnimal/fshammer/blob/main/scripts/showcase/repro/ganesha-9p-create.sh).
