#!/usr/bin/env python3
"""fshammer: a concurrent workload for a filesystem, checked against an in-memory model.

Runs every variant of every phase at least once against --dir, then repeats until --duration is spent,
and verifies what it wrote. Prints one or more lines per phase, a FAIL line per problem and a DECLARED GAP
line for anything not tested.

Phases: own files, download, malice, identity, namespace, sqlite, publish, inode, walk, locks, contest,
observer, shared. See https://github.com/PacAnimal/fshammer for what each checks.
"""
# lets signatures name types defined further down, on every supported Python
from __future__ import annotations

__version__ = "1.0.0"

import argparse, binascii, errno, hashlib, mmap, os, queue, random, shutil, stat, struct, subprocess, sys, time, traceback
from dataclasses import dataclass, field
from multiprocessing import Pipe, Process, Queue

# absent on non-Unix platforms; the locks phase then reports a gap
try:
    import fcntl
except ImportError:
    fcntl = None                                      # type: ignore[assignment]

MAGIC = b"AH"
HEADER = struct.Struct("<2sHQQI")  # magic, writer, seq, length, crc32
SHARED = "shared.bin"

# own-file write sizes, weighted toward 32 KiB; the largest crosses a typical 10 MiB write-behind cap
SIZES = ([1] * 2 + [7] * 2 + [512] * 3 + [4096] * 6 + [32768] * 40
         + [65536] * 6 + [1 << 20] * 3 + [(10 << 20) + 4096])

# shared.bin appends must fit one write(2) and one transport message, or concurrent appends may legally
# interleave inside a block
SHARED_SIZES = [1, 7, 512, 4096, 32768, 32768, 32768, 65536]
# shared.bin is never truncated, so it is capped by bytes rather than time
SHARED_BUDGET_PER_WRITER = 256 * 1024 * 1024
SHARED_MIN_BLOCKS = 200

# fractions of --duration per phase; the shared phase gets what is left
OWN_PHASE_SHARE = 0.13
DOWNLOAD_PHASE_SHARE = 0.20
MALICE_PHASE_SHARE = 0.11
IDENTITY_PHASE_SHARE = 0.08
SQLITE_PHASE_SHARE = 0.10
OBSERVER_PHASE_SHARE = 0.06
NAMESPACE_PHASE_SHARE = 0.05
PUBLISH_PHASE_SHARE = 0.06
INODE_PHASE_SHARE = 0.05
WALK_PHASE_SHARE = 0.05
LOCKS_PHASE_SHARE = 0.04
CONTEST_PHASE_SHARE = 0.04

# the file the observer phase appends to while the other writers look at it
OBSERVED = "observed.log"
OBSERVED_BUDGET = 128 * 1024 * 1024
OBSERVED_MIN_BLOCKS = 200
OBSERVED_MIN_LOOKS = 100
OBSERVED_PATIENCE = 240             # seconds before the minimums give up and say so
OBSERVED_LOOKS = ("stat", "listdir", "seek_end", "read_tail")

# a downloader writes through a 32 KiB buffered stream: whole buffers, then a residual at close
DOWNLOAD_BUFFER = 32768
DOWNLOAD_CHUNK = 4096 * 16          # one open-write-close per ranged request
PART_MODE = 0o600
DONE_MODE = 0o666                   # narrowed by the umask
# where the "full_sequence" flavour closes and reopens: mid-buffer, off every power of two
REOPEN_AT = 287 * DOWNLOAD_BUFFER + 4096
DOWNLOAD_SLOTS = 3                   # downloads in flight per writer
DOWNLOAD_TMPDIR = "dltmp"            # where a .part starts when downloads are staged in a temp dir


def download_move(src: str, dst: str):
    """rename(2), falling back to copy-then-delete on EXDEV, as a portable move does."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copyfile(src, dst)
        os.unlink(src)


# cycled in order; "full_sequence" is a downloader's whole call sequence, including the mid-download reopen
DOWNLOAD_FLAVOURS = ["full_sequence", "resume", "retarget", "oneshot", "chunked"]

# read so a finished download can be checked against the mode it really ends up with
UMASK = os.umask(0o022)
os.umask(UMASK)


# ── shapes shared between phases ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlockId:
    """A frame in shared.bin: which writer appended it, and its sequence number."""
    writer: int
    seq: int


@dataclass(frozen=True)
class AckedBlock:
    """A frame the kernel told a writer it had taken: its sequence within that writer, and its length."""
    seq: int
    length: int


@dataclass
class Downloaded:
    """What the LAST download to land on one target name left there, and how many landed there in all."""
    digest: str
    size: int
    part: str                                           # the .part path, which must no longer exist
    runs: int = 1


@dataclass(frozen=True)
class Digest:
    """One file's model, reduced to what the parent re-checks it against."""
    digest: str
    size: int


@dataclass
class DownloadOutcome:
    """One writer's download phase: the finished files, and how many each flavour completed."""
    finished: dict[str, Downloaded] = field(default_factory=dict)
    flavours: dict[str, int] = field(default_factory=lambda: {f: 0 for f in DOWNLOAD_FLAVOURS})


def payload(tag: bytes, length: int) -> bytes:
    out = bytearray()
    while len(out) < length:
        out += binascii.crc32(tag + str(len(out)).encode()).to_bytes(4, "little")
    return bytes(out[:length])


def write_all(fd: int, data: bytes) -> None:
    """Loops over short writes; on an O_APPEND fd the remainder still lands contiguously at the end."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        offset += written
        view = view[written:]


def block(writer: int, seq: int, length: int) -> bytes:
    body = payload(f"{writer}:{seq}:".encode(), length)
    return HEADER.pack(MAGIC, writer, seq, length, binascii.crc32(body)) + body


# each writer works a spread of files with different write patterns; the names appear in failure reports
FILE_KINDS = ["dribble", "bulk", "boundary", "churn", "sparse", "tiny", "overlap",
              "mixed", "mixed", "mixed", "mixed"]
# model size caps (MAX_MODEL_* below): bulk and boundary get room to cross a 10 MiB cap several times

# kinds that write at chosen offsets, so they need a descriptor without O_APPEND
POSITIONAL_KINDS = {"churn", "sparse", "overlap"}
# Enough file to have a middle, a page boundary and a cap boundary to straddle.
OVERLAP_SEED = 3 << 20

MAX_MODEL_PER_FILE = 8 << 20
MAX_MODEL_LARGE = 48 << 20
LARGE_KINDS = {"bulk", "boundary"}


def hammer_own(directory: str, writer: int, deadline: float, rng: random.Random,
               models: dict[str, bytearray]) -> dict[str, bytearray]:
    """Writes to a spread of files in every way that changes their bytes, mirroring each op into a model.

    Files are worked interleaved with several handles each, so a server coalescing writes per handle has to
    flush and re-anchor constantly.
    """
    files = [(os.path.join(directory, f"w{writer}-{i}-{kind}.bin"), kind)
             for i, kind in enumerate(FILE_KINDS)]
    kinds: dict[str, str] = {path: kind for path, kind in files}
    # --single-writer-handle: a writeback client cannot keep two write descriptors on one inode coherent
    single_writer = _SINGLE_WRITER_HANDLE
    # filled in place, so the models survive an exception and the files are still verified
    models.update({path: bytearray() for path, _ in files})
    handles: dict[str, list[int]] = {path: [] for path, _ in files}
    # what wrote each range, so a mismatch names the operation; bounded for long runs
    journals: dict[str, list[dict]] = {path: [] for path, _ in files}
    op = 0

    def reopen(path: str, extra: int = 0) -> None:
        for h in handles[path]:
            os.close(h)
        # O_WRONLY: a client may send an append to be placed by the server only on a write-only handle;
        # read-write turns it into a positional write. The draw is always taken, so the seed's sequence
        # does not depend on --single-writer-handle.
        count = rng.randint(1, 3)
        if single_writer and kinds[path] in POSITIONAL_KINDS:
            handles[path] = [os.open(path, os.O_WRONLY | os.O_CREAT | extra, 0o644)]
        else:
            handles[path] = [os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | extra, 0o644)
                             for _ in range(1 if single_writer else count)]

    def append(path: str, fd: int, data: bytes) -> None:
        # without O_APPEND, an append is a pwrite at the model's end
        if single_writer and kinds[path] in POSITIONAL_KINDS:
            pwrite_all(fd, data, len(models[path]))
        else:
            write_all(fd, data)
        models[path].extend(data)

    def positional_write(path: str, fd: int, data: bytes, off: int) -> None:
        """Writes at a chosen offset, through this file's own descriptor or a second one."""
        if single_writer and kinds[path] in POSITIONAL_KINDS:
            pwrite_all(fd, data, off)
            return
        wfd = os.open(path, os.O_WRONLY)
        try:
            pwrite_all(wfd, data, off)
        finally:
            os.close(wfd)

    def check(path: str, fd: int, what: str) -> None:
        actual = os.fstat(fd).st_size
        if actual != len(models[path]):
            raise AssertionError(
                f"{path}: after op {op} ({what}) the file is {actual} bytes and the model is "
                f"{len(models[path])} ({actual - len(models[path]):+d})")

    try:
        for path, _ in files:
            reopen(path)

        # every file once first, then until the deadline
        mandatory = list(files)
        while mandatory or time.monotonic() < deadline:
            path, kind = mandatory.pop() if mandatory else rng.choice(files)
            model, fd = models[path], rng.choice(handles[path])
            journal = journals[path]
            op += 1
            length_before = len(model)

            if kind == "dribble":
                # hundreds of one-byte appends, for off-by-ones in a coalescing buffer
                for i in range(rng.randint(200, 1000)):
                    one = payload(f"{writer}:{op}:{i}".encode(), 1)
                    os.write(fd, one)
                    model.extend(one)
                check(path, fd, "1-byte dribble")

            elif kind == "bulk":
                # multi-megabyte appends, several times a 10 MiB write-behind boundary some clients and servers use
                append(path, fd, payload(f"{writer}:{op}:b".encode(), rng.choice([4 << 20, 11 << 20, 24 << 20])))
                check(path, fd, "bulk append")

            elif kind == "boundary":
                # land on, just under and just over the cap, from an off-boundary start
                cap = 10 << 20
                target = rng.choice([cap, cap - 1, cap + 1, cap // 2, 2 * cap, 2 * cap - 1])
                want = target - (len(model) % cap)
                append(path, fd, payload(f"{writer}:{op}:d".encode(), max(1, want)))
                check(path, fd, "boundary append")

            elif kind == "churn":
                # truncate and overwrite far more than we append, so the file is constantly reshaped
                roll = rng.random()
                if roll < 0.35 and model:
                    new = rng.randrange(len(model) + 1)
                    os.ftruncate(fd, new)
                    del model[new:]
                    check(path, fd, "truncate down")
                elif roll < 0.60 and model:
                    off = rng.randrange(len(model))
                    data = payload(f"{writer}:{op}:o".encode(), min(rng.choice(SIZES), len(model) - off))
                    if data:
                        positional_write(path, fd, data, off)
                        model[off:off + len(data)] = data
                        journal.append({"op": op, "kind": "churn/overwrite", "start": off, "end": off + len(data)})
                        check(path, fd, "overwrite")
                elif roll < 0.75:
                    new = len(model) + rng.choice(SIZES)
                    os.ftruncate(fd, new)
                    model.extend(b"\0" * (new - len(model)))
                    check(path, fd, "truncate up")
                else:
                    append(path, fd, payload(f"{writer}:{op}:c".encode(), rng.choice(SIZES)))
                    check(path, fd, "append")

            elif kind == "sparse":
                # write past the end so the gap must read back as zeros
                gap = rng.choice([1, 4096, 32768, 1 << 20, 9 << 20])
                data = payload(f"{writer}:{op}:s".encode(), rng.choice([1, 4096, 32768]))
                positional_write(path, fd, data, len(model) + gap)
                journal.append({"op": op, "kind": "sparse/hole", "start": len(model), "end": len(model) + gap})
                model.extend(b"\0" * gap)
                journal.append({"op": op, "kind": "sparse/data", "start": len(model), "end": len(model) + len(data)})
                model.extend(data)
                check(path, fd, "sparse write")

            elif kind == "overlap":
                # writes that overwrite and extend at once, at offsets straddling EOF, 4 KiB, the 10 MiB cap or
                # one byte off any of them, overlapping each other so the last write to a byte must win
                if len(model) < OVERLAP_SEED:
                    append(path, fd, payload(f"{writer}:{op}:seed".encode(), OVERLAP_SEED))
                cap = 10 << 20
                for step in range(rng.randint(6, 16)):
                    end = len(model)
                    span = rng.choice([1, 7, 4095, 4096, 4097, 32767, 32768, 32769,
                                       (1 << 20) - 1, 1 << 20, cap - 1, cap + 1])
                    start = max(0, min(rng.choice([
                        end - 1,                                # one byte inside, all the rest new
                        end - span // 2,                        # half over the end
                        end - span,                             # exactly up to the end
                        end,                                    # pure extension, from the exact edge
                        (end // 4096) * 4096,                   # on a page boundary
                        (end // 4096) * 4096 - 1,               # one byte under one
                        end - (end % cap),                      # on the coalescing cap
                        end - (end % cap) - 1,                  # and one byte under that
                        rng.randrange(end + 1),                 # and somewhere with no meaning at all
                    ]), end))
                    data = payload(f"{writer}:{op}:o{step}:{start}".encode(), span)
                    positional_write(path, fd, data, start)
                    model[start:start + len(data)] = data
                    journal.append({"op": op, "kind": "overlap", "start": start, "end": start + len(data)})

                    # reshape mid-barrage so the next write meets a length that just moved
                    if rng.random() < 0.35:
                        to = max(0, rng.choice([len(model) // 2, len(model) - 1, len(model) + 1,
                                                len(model) + rng.choice([4096, 1 << 20]),
                                                (len(model) // 4096) * 4096]))
                        os.ftruncate(fd, to)
                        if to > len(model):
                            model.extend(bytes(to - len(model)))
                        else:
                            del model[to:]
                    check(path, fd, "overlapping write")

            elif kind == "tiny":
                for _ in range(rng.randint(5, 50)):
                    append(path, fd, payload(f"{writer}:{op}:t".encode(), rng.choice([1, 2, 3, 7, 63, 512])))
                os.write(fd, b"")                       # zero-length write: legal, must change nothing
                check(path, fd, "tiny appends")

            else:
                append(path, fd, payload(f"{writer}:{op}:m".encode(), rng.choice(SIZES)))
                check(path, fd, "mixed append")

            # journal growth here; in-place overwrites are journalled where they happen
            if len(model) > length_before:
                journal.append({"op": op, "kind": kind, "start": length_before, "end": len(model)})
            elif len(model) < length_before:
                journal.append({"op": op, "kind": f"{kind}/shrink", "start": len(model), "end": length_before})
            del journal[:-400]

            # read a window back through a fresh handle and check it against the model right now
            if model and rng.random() < 0.25:
                off = rng.randrange(len(model))
                want = bytes(model[off:off + rng.choice([1, 4096, 32768, 1 << 20])])
                rfd = os.open(path, os.O_RDONLY)
                try:
                    got = os.pread(rfd, len(want), off)
                finally:
                    os.close(rfd)
                if got != want:
                    at = next((i for i in range(min(len(got), len(want))) if got[i] != want[i]), min(len(got), len(want)))
                    bad = off + at
                    # drop the page cache and re-read: correct now means the client cache was wrong; still
                    # wrong means the bytes are not stored
                    if not CAN_DROP_CACHE:
                        # without a cache drop the re-read proves nothing
                        after_drop = ("this platform has no posix_fadvise, so the cache cannot be dropped "
                                      "and re-reading proves NOTHING either way")
                    else:
                        after_drop = "not attempted"
                        try:
                            dfd = os.open(path, os.O_RDONLY)
                            try:
                                os.posix_fadvise(dfd, 0, 0, os.POSIX_FADV_DONTNEED)
                                again = os.pread(dfd, len(want), off)
                            finally:
                                os.close(dfd)
                            after_drop = ("SAME bytes — the data really is not stored (a storage loss)" if again == got
                                          else "CORRECT after dropping the cache — the bytes were on the server "
                                               "and the client's cache was wrong (not a storage loss)"
                                          if again == want else
                                          f"DIFFERENT again: {again[at:at + 16]!r}")
                        except OSError as exc:
                            after_drop = f"could not re-read: {exc}"
                    # which operation wrote the bad byte, and the recent operations around it
                    owner = next((j for j in reversed(journal)
                                  if j["start"] <= bad < j["end"]), None)
                    recent = " | ".join(f"{j['kind']}@{j['start']}+{j['end'] - j['start']}"
                                        for j in journal[-8:])
                    raise AssertionError(
                        f"read-back mismatch in {path}: window at {off}, first bad byte "
                        f"at {bad} ({len(got)} read, {len(want)} expected); "
                        f"byte {bad} was written by {owner['kind'] if owner else 'NOTHING RECORDED'}"
                        f"{'' if not owner else f" spanning {owner['start']}..{owner['end']}"}; "
                        f"model len {len(model)}; got={got[at:at + 16]!r} want={want[at:at + 16]!r}; "
                        f"after dropping the page cache: {after_drop}; "
                        f"on the server: {server_verdict(path, model, off, want, bad)}; "
                        f"last ops: {recent}")

            if rng.random() < 0.02:
                # backdate mtime as tar, rsync -t and cp -p do; a server deriving versions from mtime goes
                # backwards. No content changes, so nothing may notice.
                old = time.time() - rng.choice([3600, 86400, 86400 * 400])
                try:
                    os.utime(path, (old, old))
                except OSError:
                    pass
                check(path, fd, "backdated mtime")

            if rng.random() < 0.30:
                os.fstat(fd)        # a stat between writes is what rewound the client's i_size
            if rng.random() < 0.04:
                os.fsync(fd)
            if rng.random() < 0.02 and hasattr(os, "fdatasync"):
                os.fdatasync(fd)    # not on every platform (macOS has none)

            if len(model) > (MAX_MODEL_LARGE if kind in LARGE_KINDS else MAX_MODEL_PER_FILE):
                os.ftruncate(fd, 0)
                del model[:]
                check(path, fd, "reset")

            if rng.random() < 0.05:                     # full read-back, mid-run
                with open(path, "rb") as fh:
                    actual = fh.read()
                if actual != bytes(model):
                    limit = min(len(actual), len(model))
                    at = next((i for i in range(limit) if actual[i] != model[i]), limit)
                    raise AssertionError(f"{path} diverged from the model at offset {at} "
                                         f"(file {len(actual)}, model {len(model)})")

            if rng.random() < 0.04:                     # O_TRUNC reopen — must come back empty
                # through reopen(), so positional kinds do not get O_APPEND
                reopen(path, os.O_TRUNC)
                del model[:]
            elif rng.random() < 0.04:                   # rename away and back, then keep writing
                moved = path + ".moved"
                for h in handles[path]:
                    os.close(h)
                handles[path] = []
                os.rename(path, moved)
                os.rename(moved, path)
                reopen(path)
            elif rng.random() < 0.25:                   # close and reopen, as a download continuation does
                reopen(path)
    finally:
        # close every handle even if an fsync fails, and report the failures together
        unflushed: list[str] = []
        for hs in handles.values():
            for h in hs:
                try:
                    os.fsync(h)
                except OSError as e:
                    unflushed.append(f"fd {h}: {errno.errorcode.get(e.errno, e.errno)}")
                finally:
                    os.close(h)
        if unflushed:
            raise AssertionError("writes were still not on the filesystem at close: " + "; ".join(unflushed))
    return models


def open_shared(path: str, refusals: dict[int, int]) -> int:
    """Opens the shared log for append, retrying and counting EEXIST/ENOENT.

    O_CREAT without O_EXCL must not fail with EEXIST, but across two mounts nothing locks both directories
    and some clients (u9fs, for one) surface the losing side of the race. Retrying keeps that writer's
    blocks in the check; the counts are reported.
    """
    while True:
        try:
            return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOENT):
                raise
            refusals[exc.errno] = refusals.get(exc.errno, 0) + 1
            if sum(refusals.values()) > 100000:
                raise AssertionError(f"the shared log could not be opened after {sum(refusals.values())} "
                                     f"refusals — this is no longer a race, it is a mount that does not work")


def hammer_shared(path: str, writer: int, deadline: float, rng: random.Random, written: list[AckedBlock],
                  refusals: dict[int, int]) -> None:
    """Many writers appending to one file with O_APPEND; every block must land whole and distinct.

    With --alt-dir, half the writers go through a second mount, a second client session. Acknowledged
    blocks go into a list the caller owns, so they are still checked if the fsync in the finally raises.
    """
    seq, fd, appended = 0, None, 0
    try:
        while appended < SHARED_BUDGET_PER_WRITER and (seq < SHARED_MIN_BLOCKS or time.monotonic() < deadline):
            if fd is None:
                fd = open_shared(path, refusals)
            for _ in range(rng.randint(1, 12)):
                length = rng.choice(SHARED_SIZES)
                frame = block(writer, seq, length)
                # ONE write, never a loop: a retry would let another appender interleave mid-block
                if os.write(fd, frame) != len(frame):
                    raise AssertionError(f"short write of a {len(frame)}-byte shared block")
                written.append(AckedBlock(seq, length))
                appended += len(frame)
                seq += 1
                if rng.random() < 0.30:
                    os.fstat(fd)
            if rng.random() < 0.35:
                # cleared before closing, so the finally does not close a released descriptor
                closing, fd = fd, None
                os.close(closing)
    finally:
        if fd is not None:
            try:
                os.fsync(fd)          # the deferred write-behind error surfaces here, and must not skip the close
            finally:
                os.close(fd)


def cannot_start(what: str, exc: OSError) -> int:
    """The filesystem failed before any workload ran: one FAIL line and a verdict, not a traceback."""
    print(f"  FAIL setup: {what}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    print("fshammer: 1 problem(s) (it could not start)")
    return 1


def usage_error(message: str) -> None:
    """A run that cannot start is an invocation problem, exit 2 like argparse, never a FAIL's exit 1."""
    print(f"fshammer: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def make_alt_dir(path: str, patience: float = 30.0) -> None:
    """Creates --alt-dir, or waits for it to appear. It is usually --dir, just created through the other mount,
    and that mount may still have the name cached as absent: mkdir then fails with EEXIST while a stat says it
    is not there."""
    try:
        os.makedirs(path, exist_ok=True)
        return
    except FileExistsError:
        pass
    deadline = time.monotonic() + patience
    while not os.path.isdir(path):
        if time.monotonic() > deadline:
            usage_error(f"--alt-dir {path}: the name exists but is not visible as a directory through this mount")
        time.sleep(0.5)


def require_same_export(primary: str, alternate: str) -> None:
    """Checks --dir and --alt-dir are the same directory by writing a sentinel through one and reading it
    through the other. Without this, a missing second mount is just an empty local directory and the run
    reports half the blocks lost."""
    token = f"fshammer-{os.getpid()}-{time.time_ns()}".encode()
    probe = os.path.join(alternate, ".fshammer-same-export")
    try:
        with open(probe, "wb") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        with open(os.path.join(primary, ".fshammer-same-export"), "rb") as fh:
            if fh.read() != token:
                usage_error("--alt-dir is not the same export as --dir: the sentinel read back wrong")
    except FileNotFoundError as exc:
        usage_error(f"--alt-dir is not the same export as --dir: {exc}")
    finally:
        try:
            os.unlink(probe)
        except FileNotFoundError:
            pass


def create_unique(path: str, mode: int) -> None:
    """Takes a name by creating it with O_EXCL, removing a leftover first."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))
    except FileExistsError:
        os.unlink(path)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))


@dataclass(frozen=True)
class DownloadPlan:
    """How big the download is, the offsets it is paused and resumed at, and where its target changes."""
    total: int
    stops: list[int]
    retarget: int = -1                                  # -1 = the file it started for is the file it ends in


def download_plan(flavour: str, rng: random.Random) -> DownloadPlan:
    if flavour == "full_sequence":
        # the residual close at 9408512, then more buffers on a reopened append fd, past 11 MiB
        return DownloadPlan(REOPEN_AT + rng.choice([64, 96, 128]) * DOWNLOAD_BUFFER + 4096,
                            [REOPEN_AT])
    if flavour == "resume":
        # paused on a buffer boundary and off one; the resumed append must start at the agreed offset
        return DownloadPlan(rng.choice([40, 72, 96]) * DOWNLOAD_BUFFER + rng.choice([0, 777, 4096]),
                            [8 * DOWNLOAD_BUFFER, 21 * DOWNLOAD_BUFFER + 1237])
    if flavour == "retarget":
        return DownloadPlan(rng.choice([48, 96, 160]) * DOWNLOAD_BUFFER + 512, [],
                            rng.choice([3, 17, 40]) * DOWNLOAD_BUFFER + 900)
    if flavour == "chunked":
        return DownloadPlan(rng.choice([24, 64, 128]) * DOWNLOAD_CHUNK + 1500, [])
    # oneshot: one open, one close, one rename. The largest is 10 MiB and crosses ten transfer boundaries.
    return DownloadPlan(rng.choice([4096, DOWNLOAD_BUFFER + 1234, 3 * DOWNLOAD_BUFFER,
                                    320 * DOWNLOAD_BUFFER + 4096]), [])


def download_once(directory: str, final: str, writer: int, seq: int, flavour: str, rng: random.Random):
    """One download in a downloader's order of filesystem operations, as a generator so several interleave.
    Returns the SHA-256, the length, and the .part path that must no longer exist."""
    plan = download_plan(flavour, rng)
    total, stops, retarget = plan.total, plan.stops, plan.retarget
    content = payload(f"{writer}:{seq}:ff".encode(), total)
    part, written, fd = final + ".part", 0, -1

    def buffers(upto: int):
        """Whole 32 KiB writes; the leftover lands only when the stream is closed."""
        nonlocal written
        while upto - written >= DOWNLOAD_BUFFER:
            write_all(fd, content[written:written + DOWNLOAD_BUFFER])
            written += DOWNLOAD_BUFFER
            yield
        if upto > written:
            write_all(fd, content[written:upto])
            written = upto

    def sized(path: str, what: str) -> None:
        """The file's length against the model's, checked far more often than a downloader would."""
        actual = os.path.getsize(path)
        if actual != written:
            raise AssertionError(f"{path}: {what} is {actual} bytes, {written} were written "
                                 f"({actual - written:+d}){server_size_verdict(path)}")

    if flavour == "chunked":
        # ranged download: no placeholder or .part, each chunk through its own create-append-close
        if os.path.exists(final) and os.path.getsize(final):
            # the response was not the range it asked for, so it starts the file over
            os.close(os.open(final, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PART_MODE))
        restart = total // 3 if rng.random() < 0.5 else -1
        while written < total:
            end = min(written + DOWNLOAD_CHUNK, total)
            chunk = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PART_MODE)
            try:
                write_all(chunk, content[written:end])
            finally:
                os.close(chunk)
            written = end
            sized(final, "the chunk append")
            if 0 <= restart <= written:
                os.close(os.open(final, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PART_MODE))
                written, restart = 0, -1
            yield
        part = ""                       # it never wrote one, so there is none to have outlived it
    else:
        # claim the final name with an empty placeholder, without truncating what is there
        os.close(os.open(final, os.O_WRONLY | os.O_CREAT | os.O_APPEND, DONE_MODE))
        yield

        if retarget >= 0:
            # staged in the temp directory: a dummy file created, checked and removed, then the .part
            part = os.path.join(directory, DOWNLOAD_TMPDIR, f"w{writer}-{seq}.bin.part")
            dummy = part[:-len(".part")]
            create_unique(dummy, PART_MODE)
            os.stat(dummy)
            os.unlink(dummy)
            create_unique(part, PART_MODE)
            yield

        fd = os.open(part, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, PART_MODE)
        # rename keeps the inode, so a failed close can say whether the file moved, was replaced or is in place
        born = identity_of(part)
        try:
            for stop in stops:
                yield from buffers(stop)
                closing, fd = fd, -1       # cleared first — see the close at the end of this function
                close_or_explain(closing, part, f"{written} byte(s) downloaded and the transfer paused", born)
                # the stream is gone, so the buffer's leftover has landed
                sized(part, "the paused download")      # how big is it now, while it is stopped
                sized(part, "the resuming download")    # and again, to build the Range header
                yield
                # resuming reads the whole partial file back in 32 KiB reads to seed the digest
                rfd = os.open(part, os.O_RDONLY)
                try:
                    at = 0
                    while True:
                        got = os.read(rfd, DOWNLOAD_BUFFER)
                        if not got:
                            break
                        if got != content[at:at + len(got)]:
                            bad = next(i for i in range(len(got)) if got[i] != content[at + i])
                            raise AssertionError(f"{part}: the resume read-back differs from the model at "
                                                 f"offset {at + bad}")
                        at += len(got)
                finally:
                    os.close(rfd)
                if at != written:
                    raise AssertionError(f"{part}: the resume read-back returned {at} bytes, {written} "
                                         f"were written ({at - written:+d})")
                fd = os.open(part, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PART_MODE)
                yield

            if retarget >= 0:
                yield from buffers(retarget)
                # retarget mid-download: remove what is in the way, rename the .part into place, reopen with
                # O_APPEND only (no O_CREAT, O_TRUNC or read-back). A client caching the size misplaces the
                # next append here.
                closing, fd = fd, -1       # cleared first — see the close at the end of this function
                close_or_explain(closing, part, "the destination was chosen mid-download", born)
                sized(part, "the download so far")
                moved = final + ".part"
                if os.path.exists(moved):
                    os.unlink(moved)
                download_move(part, moved)
                if os.path.exists(part):
                    raise AssertionError(f"{part}: still there after the rename onto {moved}")
                part = moved
                fd = os.open(part, os.O_WRONLY | os.O_APPEND)
                yield

            yield from buffers(total)
            # finish: flush the leftover and close; nothing on this path fsyncs. The descriptor is cleared
            # before close(2), which releases it even on error, so the cleanup cannot close it again and
            # replace the real error with EBADF.
            closing, fd = fd, -1
            close_or_explain(closing, part, "the whole file was downloaded", born)
        finally:
            if fd >= 0:
                os.close(fd)

        sized(part, "the finished download")
        download_move(part, final)           # rename(2), straight over the placeholder
        if os.path.exists(part):
            raise AssertionError(f"{part}: still there after the rename onto {final}")
        os.chmod(final, DONE_MODE & ~UMASK)     # what a download does when it completes
        sized(final, "the completed download")          # a last how-big-is-it, once it is done
        yield

    # only the last download per slot reaches the parent, so each is compared here before its slot is reused
    with open(final, "rb") as fh:
        actual = fh.read()
    if actual != content:
        limit = min(len(actual), total)
        at = next((i for i in range(limit) if actual[i] != content[i]), limit)
        raise AssertionError(f"{final} ({flavour}) diverged from the model at offset {at} "
                             f"(file {len(actual)}, model {total})")
    return Downloaded(hashlib.sha256(content).hexdigest(), total, part)


def hammer_download(directory: str, writer: int, deadline: float, rng: random.Random) -> DownloadOutcome:
    """Downloads several files at a time, as a downloader does, into a fixed set of names so the disk does
    not fill. Every flavour runs at least once. Returns the last download per name and per-flavour counts."""
    os.makedirs(os.path.join(directory, DOWNLOAD_TMPDIR), exist_ok=True)
    slots = [os.path.join(directory, f"w{writer}-download-{i}.bin") for i in range(DOWNLOAD_SLOTS)]
    outcome = DownloadOutcome()
    free, running, seq = list(slots), [], 0

    def launch() -> None:
        nonlocal seq
        final = free.pop(0)
        flavour = DOWNLOAD_FLAVOURS[seq % len(DOWNLOAD_FLAVOURS)]
        running.append((final, flavour, download_once(directory, final, writer, seq, flavour, rng)))
        seq += 1

    while True:
        # Every flavour once, whatever the clock says. Only then does the deadline get a vote.
        while free and (seq < len(DOWNLOAD_FLAVOURS) or time.monotonic() < deadline):
            launch()
        if not running:
            break
        at = rng.randrange(len(running))
        final, flavour, download = running[at]
        try:
            next(download)
        except StopIteration as done:
            landed = done.value
            landed.runs = outcome.finished[final].runs + 1 if final in outcome.finished else 1
            outcome.finished[final] = landed
            outcome.flavours[flavour] += 1
            del running[at]
            free.append(final)
    return outcome


# ── the malice phase ────────────────────────────────────────────────────────────────────────────────
# A handle is bound to a file, not a path: rename the file and the handle follows; put a new file at the old
# name and the handle must not see it; unlink it and the handle keeps its bytes. Refusals a protocol may make
# (hard links, EXDEV) are gaps; a variant that never completes is reported.
MALICE_PIECE = 16 << 10                 # a torrent client's piece
MALICE_SPAN = 24 << 20                  # crosses a 10 MiB write-behind boundary twice
MALICE_HOT_FILES = 20                   # open at once, round-robin, so per-handle state is under pressure
MALICE_HELD_FDS = 8                     # held across an enumeration — fewer than the names below, on purpose
MALICE_ENUMERATION_NAMES = 256          # names to enumerate past them: a listing that dwarfs what is open
MALICE_REPLACE_ROUNDS = 12              # rename-swaps a reader must never catch half-done
CONTENDED_ROUNDS = 40                   # attempts at one shared name, against every other writer

# past 4 GiB, where 32-bit offsets wrap; sparse, so cheap on filesystems with holes
FAR_OFFSET = (4 << 30) + 4096

# entries present for the whole scan, and churn beside them
READDIR_STABLE = 200
READDIR_CHURN = 60

# Two directories linking to each other hold four entries between them. Anything past this is a lap.
CYCLE_WALK_CEILING = 64

# One character per level, twenty-six levels: long in components and short in characters.
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# longest name this run creates (--max-name-length). Lower for stores that reserve part of a name, such
# as a WebDAV client caching entries with a suffix; hostile_names() derives its length cases from it.
# Read from the environment so spawned workers see it.
NAME_MAX = 255
MAX_NAME_LENGTH = int(os.environ.get("FSHAMMER_MAX_NAME_LENGTH") or NAME_MAX)

# legal names that some layer is tempted to normalise, escape, trim, glob, split or case-fold;
# hostile_names() appends the two length cases
HOSTILE_NAMES = [
    "with a space.bin", " leading-space.bin", "trailing-space .bin", "trailing-dot.bin.",
    "-leading-dash.bin", "--option-shaped.bin", ".hidden.bin", "...three-dots.bin", "..two-dots.bin",
    "with\ttab.bin", "with\nnewline.bin", "with'quote.bin", 'with"doublequote.bin',
    "with\\backslash.bin", "with*star.bin", "with?question.bin", "with[bracket].bin", "with{brace}.bin",
    "with|pipe.bin", "with;semicolon.bin", "with:colon.bin", "with#hash.bin", "with$dollar.bin",
    "with%percent.bin", "with&ampersand.bin", "with(paren).bin", "with<angle>.bin", "with=equals.bin",
    "with~tilde.bin", "with`backtick.bin", "with!bang.bin", "with@at.bin", "with^caret.bin",
    "caf\u00e9-precomposed.bin", "cafe\u0301-decomposed.bin",     # NFC and NFD of the same word
    "MiXeD-CaSe.bin", "mixed-case.bin",                          # the same name to a folding volume
    "\u00fcml\u00e4ut.bin", "\u4e2d\u6587.bin", "\U0001f4a3-emoji.bin", "\u202eRTL-override.bin",
    "\u200bzero-width.bin", "\u00a0nbsp.bin",
]


def hostile_names() -> list[str]:
    """HOSTILE_NAMES plus a name exactly at MAX_NAME_LENGTH, which must round-trip, and one byte past it,
    which must be refused rather than trimmed."""
    return [*HOSTILE_NAMES, "n" * MAX_NAME_LENGTH, "o" * (MAX_NAME_LENGTH + 1)]


def probe_the_cap(directory: str) -> bool:
    """Whether a NAME_MAX name works anyway when --max-name-length lowered the limit, so an unneeded cap is
    reported. Never fails the run."""
    probe = os.path.join(directory, "p" * NAME_MAX)
    try:
        with open(probe, "wb") as fh:
            fh.write(b"probe")
        return os.path.basename(probe) in os.listdir(directory)
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def name_lengths() -> list[int]:
    """Component lengths for long_names, up to and always including MAX_NAME_LENGTH."""
    return sorted({length for length in (64, 128, 200) if length < MAX_NAME_LENGTH} | {MAX_NAME_LENGTH})


class Refused(Exception):
    """A shape this run did not test, and why: an operation the store may legitimately decline, or a
    variant needing something this invocation lacked. Reported as a DECLARED GAP."""


# ENOTSUPP, internal to Linux with no errno name; the NFS client passes NFS3ERR_NOTSUPP through as 524
LINUX_ENOTSUPP = 524

# what each operation may legitimately refuse. ENOENT, ESTALE, EBADF and EINVAL never are: every operation
# acts on a file this process just created. Making a name is optional: a store may have no symlinks or hard
# links, and must say so.
NAME_MAKING_REFUSALS = {errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS, LINUX_ENOTSUPP}

# between two directories only; EXDEV for a same-directory rename is a finding
CROSS_DIRECTORY_RENAME_REFUSALS = {errno.EXDEV}

NOTHING_IS_REFUSABLE: set[int] = set()

# ordinary POSIX names: only a length limit is a legitimate refusal
REFUSALS = {errno.ENAMETOOLONG}

# only where this phase destroyed the file itself (unlinked, replaced, or removed its directory). Clients
# can report ESTALE as EIO, so the check is that the write did not land and no other file changed.
DESTROYED_REFUSALS = {errno.ENOENT, errno.ESTALE, errno.EBADF, errno.EIO}


def entitlement(fn, args) -> set[int]:
    """What THIS call may refuse. Derived from the operation, and for a rename from its two directories."""
    # every os.symlink and os.link goes through allowed(), scaffolding included, so a store without links
    # gets a gap everywhere rather than a FAIL in some variants
    if fn in (os.symlink, os.link):
        return NAME_MAKING_REFUSALS
    if fn is os.rename and os.path.dirname(args[0]) != os.path.dirname(args[1]):
        return CROSS_DIRECTORY_RENAME_REFUSALS
    return NOTHING_IS_REFUSABLE


def allowed(fn, *args, refusals: set[int] | None = None, **kwargs):
    """Run fn. A refusal THIS operation is entitled to raises Refused; anything else propagates."""
    entitled = refusals if refusals is not None else entitlement(fn, args)
    try:
        return fn(*args, **kwargs)
    except OSError as e:
        if e.errno in entitled:
            named = fn.__name__ if hasattr(fn, "__name__") else fn
            raise Refused(f"the store refused {named} with {errno.errorcode.get(e.errno, e.errno)} "
                          f"({e.strerror}), which it is entitled to do") from e
        raise


CAN_DROP_CACHE = hasattr(os, "posix_fadvise")


def audited_contents(path: str, what: str) -> bytes | None:
    """The audit's read. An OSError is printed as a FAIL and None returned, so one unreadable file does not
    stop the audit or kill the process."""
    try:
        return contents(path)
    except OSError as ex:
        print(f"  FAIL {path}: {what} could not be read back — {ex.__class__.__name__}: {ex}",
              file=sys.stderr)
        return None


def contents(path: str) -> bytes:
    """Reads a file after evicting its clean pages with POSIX_FADV_DONTNEED, so the bytes come from the
    server rather than the page cache. Without posix_fadvise this is a plain read, reported once as a gap."""
    with open(path, "rb") as fh:
        if CAN_DROP_CACHE:
            try:
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        return fh.read()


def read_plainly(path: str) -> bytes:
    """Reads a file through the page cache. Compared with contents(): a fault only this read sees is the
    client's; one both see is the store's."""
    with open(path, "rb") as fh:
        return fh.read()


def via_alt_mount(path: str) -> str:
    """The same path through --alt-dir, or unchanged without it."""
    if not _ALT_ROOT or not _MOUNT_ROOT:
        return path
    return os.path.join(_ALT_ROOT, os.path.relpath(path, _MOUNT_ROOT))


def sizes_agree(path: str, fd: int, what: str, expected: int) -> None:
    """fstat(fd) and stat(path) must both match the model's length; they share one inode, so only the
    model is an independent reference."""
    by_handle = os.fstat(fd).st_size
    if by_handle != expected:
        raise AssertionError(f"{path}: {what} — the handle says {by_handle} bytes, we wrote {expected}")
    try:
        by_path = os.stat(path).st_size
    except FileNotFoundError:
        return                          # unlinked on purpose by the caller; the handle's view is all there is
    if by_path != expected:
        raise AssertionError(f"{path}: {what} — the name says {by_path} bytes, we wrote {expected}")


def mmap_diagnosis(fh) -> str:
    """Why a mapping was refused: each access mode is tried in turn and the mount's options are read back."""
    modes = []
    for name, access in (("shared", mmap.ACCESS_WRITE), ("private", mmap.ACCESS_COPY), ("read", mmap.ACCESS_READ)):
        try:
            probe = mmap.mmap(fh.fileno(), 0, access=access)
        except (OSError, ValueError) as exc:
            modes.append(f"{name}={type(exc).__name__}:{exc}")
        else:
            probe.close()
            modes.append(f"{name}=ok")
    return "; ".join(modes) + "; " + raw_mmap_errno(fh) + "; " + mount_options(fh.name)


def raw_mmap_errno(fh) -> str:
    """The errno of a raw mmap(2), for shared and private mappings. CPython's wrapper can raise before
    reaching the kernel."""
    try:
        import ctypes, ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.mmap.restype = ctypes.c_void_p
        libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
        libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        prot_read, prot_write, map_shared, map_private = 0x1, 0x2, 0x01, 0x02
        length = os.fstat(fh.fileno()).st_size or 4096
        out = []
        for name, prot, flags in (("raw-shared-rw", prot_read | prot_write, map_shared),
                                  ("raw-shared-ro", prot_read, map_shared),
                                  ("raw-private-rw", prot_read | prot_write, map_private)):
            ctypes.set_errno(0)
            addr = libc.mmap(None, length, prot, flags, fh.fileno(), 0)
            if addr in (None, ctypes.c_void_p(-1).value):
                err = ctypes.get_errno()
                out.append(f"{name}=errno {err} {os.strerror(err)}")
            else:
                libc.munmap(ctypes.c_void_p(addr), length)
                out.append(f"{name}=ok")
        return "; ".join(out)
    except Exception as exc:                                    # a probe must never break the phase it explains
        return f"raw mmap probe unavailable: {exc!r}"


def mount_options(path: str) -> str:
    """The options of the mount the path lives on, longest matching mount point wins."""
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as mounts:
            best = None
            for line in mounts:
                fields = line.split()
                if len(fields) < 4:
                    continue
                point = fields[1]
                if (path == point or path.startswith(point.rstrip("/") + "/")) and (best is None or len(point) > len(best[1])):
                    best = (fields[2], point, fields[3])
        return f"mounted {best[0]} at {best[1]} with {best[2]}" if best else "no mount found for the path"
    except OSError as exc:
        return f"mount options unreadable: {exc}"


def variant_cycle(variants, seed: int, writer: int, procs: int) -> list[str]:
    """A per-writer rotation of one seeded variant order, so writers start at different points and together
    cover every variant rather than overlapping by chance. Popped from the end."""
    order = list(variants)
    random.Random(seed).shuffle(order)
    offset = (writer * len(order)) // max(1, procs)
    rotated = order[offset:] + order[:offset]
    rotated.reverse()
    return rotated


# seconds between progress lines, so a slow run is distinguishable from a wedged one
HEARTBEAT_SECONDS = 5

_last_beat = 0.0


def beat(writer: int, phase: str, detail: str) -> None:
    """One line saying work COMPLETED — and the line that stops arriving names what hung."""
    global _last_beat
    now = time.monotonic()
    if now - _last_beat < HEARTBEAT_SECONDS:
        return
    _last_beat = now
    print(f"  w{writer} {phase}/{detail}", file=sys.stderr, flush=True)


@dataclass
class PhaseResult:
    """One writer's turn at a variant-rotating phase: completions, failures (first kept) and refusals.
    `done` is seeded with every variant, so one that never ran shows as zero."""
    done: dict[str, int]
    failures: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)

    @classmethod
    def over(cls, variants) -> "PhaseResult":
        return cls(done={v: 0 for v in variants})

    def absorb(self, other: "PhaseResult") -> None:
        """Folds another writer's turn in. First failure wins, for the reason given above."""
        for variant, count in other.done.items():
            self.done[variant] = self.done.get(variant, 0) + count
        for variant, why in other.failures.items():
            self.failures.setdefault(variant, why)
        for variant, why in other.refused.items():
            self.refused.setdefault(variant, why)

    def record(self, variant: str, exc: BaseException, frames: int = 2) -> None:
        """Files an exception under its variant, with where it was raised."""
        where = " <- ".join(f"{f.name}:{f.lineno} {(f.line or '').strip()[:60]}"
                            for f in traceback.extract_tb(exc.__traceback__)[-frames:])
        self.failures.setdefault(variant, f"{type(exc).__name__}: {exc} [{where}]")


def mandatory_then_repeat(variants, seed: int, writer: int, procs: int, deadline: float, phase: str = ""):
    """Yields every variant once regardless of the clock, then repeats until the deadline."""
    # filtered once: a phase with every variant declared would otherwise refill and spin until the deadline
    testable = [v for v in variants if not declared_gap(phase, v)]
    order = variant_cycle(testable, seed, writer, procs)
    for variant in reversed(order):
        yield variant
        beat(writer, phase, variant)        # resumed here means that variant is DONE, however it ended
    while time.monotonic() < deadline:
        if not order:
            order = variant_cycle(testable, seed, writer, procs)
            if not order:
                return          # no variants at all: nothing to repeat, and pop() would raise
        repeated = order.pop()
        yield repeated
        beat(writer, phase, repeated)


def hammer_sqlite(directory: str, writer: int, deadline: float, rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """SQLite's file operations, without SQLite.

      sidecar   a commit fsyncs two files in order: the -wal sidecar, then the database
      size      every write is followed by a size check, since SQLite decides commits by reading lengths
      mmap      a shared read mapping must see writes made through another descriptor, and writes through
                a shared mapping must reach the server (the -shm file)
    """
    mapping = () if _NO_MAP else ("mmap_sees_write", "mmap_write_through")
    result = PhaseResult.over(("sidecar_commit", "size_after_write", *mapping))
    seq = 0
    d = os.path.join(directory, f"w{writer}-sqlite")
    os.makedirs(d, exist_ok=True)

    def commit(main: str, sidecar: str, body: bytes, journal: bytes) -> None:
        """The order is the whole point: sidecar durable BEFORE the main file learns about it."""
        wal_fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            was = os.fstat(wal_fd).st_size
            write_all(wal_fd, journal)
            os.fsync(wal_fd)
            sizes_agree(sidecar, wal_fd, "after the journal write", was + len(journal))
        finally:
            os.close(wal_fd)

        db_fd = os.open(main, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            was = os.fstat(db_fd).st_size
            pwrite_all(db_fd, body, 0)
            os.fsync(db_fd)
            got = os.fstat(db_fd).st_size
            if got < len(body):
                raise AssertionError(f"{main}: committed {len(body)} bytes and the handle reports {got}")
            # pwrite at 0 overwrites and never truncates, so a shorter body leaves the old tail in place
            sizes_agree(main, db_fd, "after the commit", max(was, len(body)))
        finally:
            os.close(db_fd)

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "sqlite"):
        seq += 1
        base = os.path.join(d, f"{variant}-{seq}")
        try:
            if variant == "sidecar_commit":
                pages = rng.randint(2, 12)
                for step in range(pages):
                    body = payload(f"db{writer}-{seq}-{step}".encode(), 4096 * rng.randint(1, 8))
                    commit(base + ".db", base + ".db-wal", body, payload(b"j", 512))
                if not os.path.exists(base + ".db-wal"):
                    raise AssertionError("the sidecar vanished across the commit cycle")

            elif variant == "mmap_write_through":
                # writes through a shared mapping must reach the server; the kernel decides when they go
                body = payload(b"mapw", 4096 * rng.randint(2, 8))
                with open(base + ".shm", "wb") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                with open(base + ".shm", "r+b") as rw:
                    try:
                        mapped = mmap.mmap(rw.fileno(), len(body))
                    except OSError as exc:
                        result.failures.setdefault(variant, f"the mount cannot map a writable handle, and mapping is not an "
                                                    f"optional extra: {exc} ({mmap_diagnosis(rw)})")
                        continue
                    try:
                        stamp = payload(b"through-the-mapping", 2048)
                        at = rng.choice([0, 4095, 4096, len(body) - len(stamp)])
                        mapped[at:at + len(stamp)] = stamp
                        mapped.flush()
                    finally:
                        mapped.close()

                # fresh descriptor and dropped cache, so the answer comes from the server
                landed = contents(base + ".shm")
                if len(landed) != len(body):
                    raise AssertionError(f"{base}.shm: {len(landed)} bytes after a mapped write, "
                                         f"expected {len(body)} ({len(landed) - len(body):+d})")
                if landed[at:at + len(stamp)] != stamp:
                    raise AssertionError(
                        f"{base}.shm: bytes written through a shared mapping and msynced never reached the "
                        f"server — the file still reads {landed[at:at + 16]!r} at {at}")
                expected = bytearray(body)
                expected[at:at + len(stamp)] = stamp
                if landed != bytes(expected):
                    raise AssertionError(f"{base}.shm: a mapped write changed bytes outside the range it wrote")

            elif variant == "size_after_write":
                # many appends on one descriptor, length checked after each through handle and name
                fd = os.open(base + ".db", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    total = 0
                    for _ in range(rng.randint(4, 24)):
                        chunk = payload(b"pg", 4096 * rng.randint(1, 4))
                        write_all(fd, chunk)
                        total += len(chunk)
                        got = os.fstat(fd).st_size
                        if got != total:
                            raise AssertionError(f"wrote {total} bytes and the handle reports {got}")
                        sizes_agree(base + ".db", fd, "mid-transaction", total)
                    os.fsync(fd)
                    if os.stat(base + ".db").st_size != total:
                        raise AssertionError("the length changed across fsync")
                finally:
                    os.close(fd)

            elif variant == "mmap_sees_write":
                # two variants that fail independently. Some clients refuse any shared mapping of a writable
                # descriptor, so the read half maps a read-only one: it must see a write made through another
                # descriptor. Writing through the mapping is its own variant.
                body = payload(b"map", 4096 * rng.randint(2, 8))
                with open(base + ".db", "wb") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                with open(base + ".db", "rb") as ro:
                    try:
                        mapped = mmap.mmap(ro.fileno(), len(body), access=mmap.ACCESS_READ)
                    except ValueError as exc:
                        # ValueError means the mapping is longer than the file size the kernel holds, i.e. the
                        # store under-reports a file it already acknowledged and fsynced: a failure, not a gap
                        raise AssertionError(
                            f"the mount reports {os.fstat(ro.fileno()).st_size} bytes for a file we wrote and "
                            f"fsynced {len(body)} bytes to — the length was revised DOWN under us ({exc})"
                        ) from exc
                    except OSError as exc:
                        result.failures.setdefault(variant, f"the mount cannot map files at all, and mapping is not an "
                                                    f"optional extra: {exc} ({mmap_diagnosis(ro)})")
                        continue
                    try:
                        if mapped[:len(body)] != body:
                            raise AssertionError("the mapping does not hold what was written through the descriptor")
                        # a write through another descriptor must show up in the mapping
                        marker = b"COMMITED"
                        with open(base + ".db", "r+b") as w:
                            w.write(marker)
                            w.flush()
                            os.fsync(w.fileno())
                        if mapped[:len(marker)] != marker:
                            raise AssertionError(
                                f"a mapping of the file still reads {mapped[:len(marker)]!r} after a "
                                f"separate descriptor wrote and fsynced {marker!r} over the same bytes"
                            )
                    finally:
                        mapped.close()
                if contents(base + ".db")[:8] != b"COMMITED":
                    raise AssertionError("the write that the mapping saw is not in the file")
                # writing through a shared mapping, attempted every time
                with open(base + ".db", "r+b") as rw:
                    try:
                        writable = mmap.mmap(rw.fileno(), len(body))
                    except OSError as exc:
                        result.failures.setdefault("mmap_write_through",
                                                   f"the mount refuses shared writable mappings, and that is "
                                                   f"the -shm file's whole job: {exc} ({mmap_diagnosis(rw)})")
                    else:
                        try:
                            writable[0:8] = b"THROUGH!"
                            writable.flush()
                        finally:
                            writable.close()
                        if contents(base + ".db")[:8] != b"THROUGH!":
                            raise AssertionError("a write through the mapping never reached the file")

            result.done[variant] += 1
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)

    return result


def identity_of(path: str) -> tuple[int, int] | None:
    """(dev, ino) of what a name points at now, or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def explain_fd_failure(failed: OSError, path: str, what: str,
                       born: tuple[int, int] | None = None) -> OSError:
    """Whether the file went or only the handle, after an fd operation failed.

    The same ESTALE means a handle to a deleted file (fine) or a live file's lost handle (data loss). Returns
    an OSError with the original errno and the diagnosis in the message.
    """
    try:
        st = os.lstat(path)
    except OSError as probe:
        gone = errno.errorcode.get(probe.errno, str(probe.errno))
        return OSError(failed.errno, f"{failed.strerror} — {path}: {what}, and the path is GONE ({gone}), so "
                                     f"refusing a handle to it is honest")

    moved = "" if born is None or born == (st.st_dev, st.st_ino) else \
        f" and it is a DIFFERENT file now (was dev {born[0]} ino {born[1]})"
    return OSError(failed.errno, f"{failed.strerror} — {path}: {what}, but the path is STILL THERE "
                                 f"(dev {st.st_dev}, ino {st.st_ino}){moved}: the file did not go, the "
                                 f"HANDLE did, and the bytes already acknowledged cannot be re-sent")


def fsync_or_explain(fd: int, path: str, what: str, born: tuple[int, int] | None = None) -> None:
    """fsync, explaining a failure as explain_fd_failure does."""
    try:
        os.fsync(fd)
    except OSError as failed:
        raise explain_fd_failure(failed, path, what, born) from failed


def close_or_explain(fd: int, path: str, what: str, born: tuple[int, int] | None = None) -> None:
    """Close, explaining a failure as explain_fd_failure does.

    A failed close has already lost the acknowledged bytes, and POSIX gives no way back from a descriptor
    to a path. If the path is gone, ESTALE is honest; if it is still there with the same dev/ino, the handle
    was lost for nothing. Raised with the original errno.
    """
    try:
        os.close(fd)
    except OSError as failed:
        raise explain_fd_failure(failed, path, f"close failed after {what}", born) from failed


def write_whole(path: str, data: bytes) -> None:
    """Replace a file's entire contents. O_TRUNC then one positional write, so the file is exactly `data`."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        pwrite_all(fd, data, 0)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Subtree:
    """What was planted under a root: file bodies and link targets by path relative to it, so the tree can be
    checked again after it moves."""
    files: dict[str, bytes]
    links: dict[str, str]


def plant_subtree(root: str) -> Subtree:
    """A tree worth moving, and its manifest: files at several depths, an empty subdirectory, a relative
    symlink and an absolute one, whose target text must survive the move unchanged."""
    files = {
        "top.bin": payload(b"top", 4096),
        os.path.join("one", "middle.bin"): payload(b"middle", 32768),
        os.path.join("one", "two", "deep.bin"): payload(b"deep", 512),
        os.path.join("one", "two", "three", "deeper.bin"): payload(b"deeper", 65536),
    }
    for relative, body in files.items():
        full = os.path.join(root, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(body)
    os.makedirs(os.path.join(root, "one", "empty"), exist_ok=True)

    links = {
        os.path.join("one", "relative-link"): os.path.join("two", "deep.bin"),
        os.path.join("one", "two", "absolute-link"): os.path.abspath(os.path.join(root, "top.bin")),
    }
    for relative, target in links.items():
        allowed(os.symlink, target, os.path.join(root, relative))
    return Subtree(files, links)


def check_subtree(root: str, planted: Subtree) -> None:
    """Everything the manifest promised, at the tree's new address."""
    for relative, body in planted.files.items():
        got = contents(os.path.join(root, relative))
        if got != body:
            raise AssertionError(f"{os.path.join(root, relative)}: {len(got)} bytes after the move, "
                                 f"expected {len(body)} — moving the tree did not bring this with it")
    empty = os.path.join(root, "one", "empty")
    if not os.path.isdir(empty):
        raise AssertionError(f"{empty}: the empty subdirectory did not survive the move")
    if not_sidecars(os.listdir(empty)):
        raise AssertionError(f"{empty}: an empty directory arrived holding {os.listdir(empty)}")
    for relative, target in planted.links.items():
        full = os.path.join(root, relative)
        if not stat.S_ISLNK(os.lstat(full).st_mode):
            raise AssertionError(f"{full}: arrived as something other than a symlink")
        if os.readlink(full) != target:
            raise AssertionError(f"{full}: names {os.readlink(full)!r} after the move, and named "
                                 f"{target!r} before it — a link's text is not the mover's to rewrite")


def hammer_namespace(directory: str, writer: int, deadline: float,
                     rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Symlinks, deep trees, long paths and wide directories.

    The other phases write a few files two levels deep, which leaves path handling mostly untested: lookups
    chained across many components, handles re-prefixed when a directory far above them is renamed, and
    directory reads spanning many reply windows. Refusals a store may legitimately make (no hard links, EXDEV)
    are recorded as gaps.
    """
    result = PhaseResult.over(("symlink_roundtrip", "symlink_dangling", "symlink_listed", "symlink_renamed",
                               "symlink_component", "symlink_into_mount", "symlink_cycle_abuse",
                               "hardlink", "populated_subtree_moves", "alphabet_path",
                               "rename_into_own_subtree", "hostile_names", "deep_tree",
                               "deep_rename_above", "wide_directory", "long_names",
                               "normalisation_is_consistent", "case_is_consistent", "path_is_not_truncated",
                               "extended_attributes"))
    base = os.path.join(directory, f"w{writer}-namespace")
    os.makedirs(base, exist_ok=True)
    seq = 0

    def fresh(kind: str) -> str:
        return os.path.join(base, f"{kind}-{seq}")

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "namespace"):
        seq += 1
        # removed however the iteration ends, so a refused rename does not leave a tree per repeat
        created_roots: list[str] = []
        try:
            if variant == "symlink_roundtrip":
                # the target is a string that must round-trip byte for byte, including odd ones
                target_file = fresh("sym-target") + ".bin"
                created_roots.append(target_file)
                body = payload(b"symtarget", 4096)
                write_whole(target_file, body)
                for target in (os.path.basename(target_file), target_file, "./" + os.path.basename(target_file),
                               "a b/c\td", "../" * 8 + "escaped"):
                    link = fresh("sym") + f"-{abs(hash(target)) % 9973}"
                    allowed(os.symlink, target, link)
                    back = os.readlink(link)
                    if back != target:
                        raise AssertionError(f"readlink returned {back!r} for a symlink created with {target!r}")
                    os.unlink(link)

            elif variant == "symlink_dangling":
                # a dangling link is legal: lstat sees it, stat does not, and the target round-trips
                link = fresh("dangling")
                missing = fresh("no-such-file")
                allowed(os.symlink, missing, link)
                if os.readlink(link) != missing:
                    raise AssertionError("a dangling symlink lost its target")
                if not os.path.islink(link):
                    raise AssertionError("a dangling symlink does not report as a link")
                if os.path.exists(link):
                    raise AssertionError("a symlink to a missing file reports that the file exists")
                os.lstat(link)                      # must succeed where stat does not
                os.unlink(link)

            elif variant == "symlink_listed":
                # A symlink among ordinary entries in a listing, and it must not be confused with its target.
                d = fresh("symdir")
                os.makedirs(d, exist_ok=True)
                real = os.path.join(d, "real.bin")
                write_whole(real, payload(b"listed", 512))
                names = []
                for i in range(8):
                    link = os.path.join(d, f"link-{i}")
                    allowed(os.symlink, "real.bin", link)
                    names.append(f"link-{i}")
                listed = set(os.listdir(d))
                missing = [n for n in names if n not in listed]
                shutil.rmtree(d, ignore_errors=True)
                if missing:
                    raise AssertionError(f"a listing of {d} omitted {len(missing)} symlink(s): {missing[:3]}")
                if "real.bin" not in listed:
                    raise AssertionError("a listing lost the file the symlinks point at")

            elif variant == "symlink_renamed":
                link, moved = fresh("sym-before"), fresh("sym-after")
                allowed(os.symlink, "some/target/path", link)
                allowed(os.rename, link, moved)
                if os.readlink(moved) != "some/target/path":
                    raise AssertionError("renaming a symlink changed what it points at")
                os.unlink(moved)

            elif variant == "symlink_component":
                # a symlink as a directory component of a path being written through
                real_dir = fresh("realdir")
                os.makedirs(real_dir, exist_ok=True)
                link_dir = fresh("linkdir")
                allowed(os.symlink, os.path.basename(real_dir), link_dir)
                body = payload(b"through-a-link", 8192)
                through = os.path.join(link_dir, "written-through.bin")
                write_whole(through, body)
                landed = os.path.join(real_dir, "written-through.bin")
                ok = contents(landed) == body
                allowed(os.unlink, link_dir)
                shutil.rmtree(real_dir, ignore_errors=True)
                if not ok:
                    raise AssertionError("bytes written through a symlinked directory did not reach the real file")

            elif variant == "symlink_into_mount":
                # an absolute target inside the served filesystem itself. A server that resolves it on its
                # own side re-enters the mount and blocks on itself, stopping service for every client.
                # Dead, live and self-naming flavours fail differently. Checked for answering at all.
                into = fresh("into-mount")
                created_roots.append(into)
                os.makedirs(into, exist_ok=True)
                real = os.path.join(into, "real.bin")
                body = payload(b"into-mount", 4096)
                with open(real, "wb") as fh:
                    fh.write(body)
                live, dead, loop = (os.path.join(into, n) for n in ("live", "dead", "loop"))
                allowed(os.symlink, os.path.abspath(real), live)
                allowed(os.symlink, os.path.abspath(os.path.join(into, "no-such-target")), dead)
                allowed(os.symlink, os.path.abspath(loop), loop)

                # The listing first, because it is the operation a user runs without thinking about it.
                listed = set(os.listdir(into))
                if not {"real.bin", "live", "dead", "loop"} <= listed:
                    raise AssertionError(f"{into}: listing lost an entry — got {sorted(listed)}")
                for link in (live, dead, loop):
                    if not stat.S_ISLNK(os.lstat(link).st_mode):
                        raise AssertionError(f"{link}: described as something other than a symlink")
                    if not os.readlink(link).startswith(os.path.abspath(into)):
                        raise AssertionError(f"{link}: readlink gave {os.readlink(link)!r}, not a path in {into}")
                if contents(live) != body:
                    raise AssertionError(f"{live}: reading THROUGH the link did not give the target's bytes")

                # a metadata write through the link: the mode must land on the target and the link must be
                # untouched
                before_link = stat.S_IMODE(os.lstat(live).st_mode)
                os.chmod(live, 0o640)
                if stat.S_IMODE(os.stat(real).st_mode) != 0o640:
                    raise AssertionError(f"{live}: chmod through the link did not reach {real}")
                if stat.S_IMODE(os.lstat(live).st_mode) != before_link:
                    raise AssertionError(f"{live}: chmod through the link changed the LINK's own mode")

                # dangling must refuse; self-naming must refuse rather than resolve forever
                for link, expected in ((dead, (errno.ENOENT,)), (loop, (errno.ELOOP, errno.ENOENT))):
                    try:
                        os.chmod(link, 0o640)
                    except OSError as err:
                        if err.errno not in expected:
                            raise AssertionError(
                                f"{link}: chmod through it failed with {errno.errorcode.get(err.errno, err.errno)}, "
                                f"not one of {[errno.errorcode[e] for e in expected]}") from None
                    else:
                        raise AssertionError(f"{link}: chmod through a link that resolves to nothing reported success")

            elif variant == "symlink_cycle_abuse":
                # A -> B -> C -> A. None of these may hang: the kernel gives up with ELOOP, but a server that
                # resolves paths itself can loop or block on itself. Mostly checks that an answer arrives and
                # that each call lands in its group: must refuse, must succeed without following, or must
                # quietly answer no.
                ring = fresh("cycle")
                created_roots.append(ring)
                os.makedirs(ring, exist_ok=True)
                a, b, c = (os.path.join(ring, n) for n in ("a", "b", "c"))
                allowed(os.symlink, os.path.abspath(b), a)
                allowed(os.symlink, os.path.abspath(c), b)
                allowed(os.symlink, os.path.abspath(a), c)
                spare = os.path.join(ring, "spare.bin")
                write_whole(spare, payload(b"spare", 1024))

                refusals = (errno.ELOOP, errno.ENOTDIR, errno.ENOENT, errno.ENAMETOOLONG)

                # ── must refuse: following the ring is the only way any of these could answer ──
                for what, act in (
                    ("cat a", lambda: contents(a)),                             # open(2) into the ring
                    ("cat b", lambda: contents(b)),                             # entering at another hop
                    ("cat c", lambda: contents(c)),                             # and at the third
                    ("stat a", lambda: os.stat(a)),                             # the FOLLOWING stat
                    ("stat b", lambda: os.stat(b)),
                    ("stat c", lambda: os.stat(c)),
                    ("statvfs a", lambda: os.statvfs(a)),
                    ("cd a", lambda: os.listdir(a)),                            # opendir(2) into the ring
                    ("chdir a", lambda: os.chdir(a)),                           # fails, so the cwd never moves
                    ("cat a/x", lambda: contents(os.path.join(a, "x"))),        # a ring as a path COMPONENT
                    ("cd a/x", lambda: os.listdir(os.path.join(a, "x"))),
                    ("realpath a", lambda: os.path.realpath(a, strict=True)),
                    ("open a rdonly", lambda: os.close(os.open(a, os.O_RDONLY))),
                    ("open a nofollow", lambda: os.close(os.open(a, os.O_RDONLY | os.O_NOFOLLOW))),
                    ("open a for write", lambda: os.close(os.open(a, os.O_WRONLY | os.O_CREAT, 0o644))),
                    ("truncate a", lambda: os.truncate(a, 0)),                  # SETATTR through the ring
                    ("chmod a", lambda: os.chmod(a, 0o644)),
                    ("utime a", lambda: os.utime(a, (1, 1))),
                    ("copy a", lambda: shutil.copy(a, os.path.join(ring, "copied.bin"))),
                    ("copy onto a", lambda: shutil.copy(spare, a)),
                    ("rename onto a/x", lambda: os.rename(spare, os.path.join(a, "landed.bin"))),
                    # opening for write through a ring must fail at the open
                    ("write a wb", lambda: open(a, "wb").close()),
                    ("write a ab", lambda: open(a, "ab").close()),
                    ("write a r+b", lambda: open(a, "r+b").close()),
                    ("write a bytes", lambda: write_whole(a, payload(b"ring", 4096))),
                    ("write a/x", lambda: write_whole(os.path.join(a, "x"), payload(b"ring", 64))),
                    ("append fd a", lambda: os.close(os.open(a, os.O_WRONLY | os.O_APPEND))),
                    ("write b", lambda: write_whole(b, payload(b"ring", 512))),
                    ("write c", lambda: write_whole(c, payload(b"ring", 512))),
                    ("mkdir a/x", lambda: os.mkdir(os.path.join(a, "x"))),
                    ("scandir is_file", lambda: [e.is_file() for e in os.scandir(ring) if e.name in ("a", "b", "c")]),
                    ("scandir is_dir", lambda: [e.is_dir() for e in os.scandir(ring) if e.name in ("a", "b", "c")]),
                ):
                    try:
                        act()
                    except OSError as e:
                        if e.errno not in refusals:
                            raise AssertionError(
                                f"{a}: `{what}` through a ring of three links failed with "
                                f"{errno.errorcode.get(e.errno, e.errno)} — not a refusal to follow it") from e
                    else:
                        raise AssertionError(f"{a}: `{what}` SUCCEEDED through a ring of three symlinks")

                # ── predicates that swallow the error must answer no, not hang
                for what, act in (
                    ("exists a", lambda: os.path.exists(a)),
                    ("isfile a", lambda: os.path.isfile(a)),
                    ("isdir a", lambda: os.path.isdir(a)),
                    ("access a", lambda: os.access(a, os.R_OK)),
                    ("access a W", lambda: os.access(a, os.W_OK)),
                    ("scandir is_symlink", lambda: [not e.is_symlink() for e in os.scandir(ring)
                                                    if e.name in ("a", "b", "c")]),
                ):
                    got = act()
                    if got is True or (isinstance(got, list) and any(got)):
                        raise AssertionError(f"{a}: `{what}` answered {got!r} — a ring resolves to nothing, "
                                             f"so anything but no means something followed it and liked what it found")

                # ── must succeed: not one of these follows anything ──
                for link, target in ((a, b), (b, c), (c, a)):
                    if not stat.S_ISLNK(os.lstat(link).st_mode):
                        raise AssertionError(f"{link}: a link in a ring is not described as a symlink")
                    if not stat.S_ISLNK(os.stat(link, follow_symlinks=False).st_mode):
                        raise AssertionError(f"{link}: stat(follow_symlinks=False) disagrees with lstat")
                    if os.path.basename(os.readlink(link)) != os.path.basename(target):
                        raise AssertionError(f"{link}: readlink gave {os.readlink(link)!r}, expected {target!r}")
                    if not os.path.islink(link):
                        raise AssertionError(f"{link}: islink says no")
                if not {"a", "b", "c"} <= set(os.listdir(ring)):
                    raise AssertionError(f"{ring}: listing a directory of looping links lost one of them")

                # renaming a link moves the link
                renamed = os.path.join(ring, "a-renamed")
                os.rename(a, renamed)
                if os.readlink(renamed) != os.path.abspath(b):
                    raise AssertionError(f"{renamed}: renaming a looping link changed what it names")
                os.rename(renamed, a)

                # renaming onto a symlink replaces the link; following it would write through the ring, or
                # over the file a live link names
                stand_in = os.path.join(ring, "stand-in.bin")
                body = payload(b"stand-in", 2048)
                write_whole(stand_in, body)
                os.replace(stand_in, a)
                if os.path.islink(a):
                    raise AssertionError(f"{a}: renaming a file onto a link left a link behind")
                if contents(a) != body:
                    raise AssertionError(f"{a}: renaming a file onto a link did not put the file there")
                os.unlink(a)
                allowed(os.symlink, os.path.abspath(b), a)               # ring restored

                # with one hop removed it is an ordinary dangling link: ENOENT, not ELOOP
                os.unlink(c)
                try:
                    contents(a)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise AssertionError(f"{a}: with the ring broken, reading it gave "
                                             f"{errno.errorcode.get(e.errno, e.errno)} and not ENOENT") from e
                else:
                    raise AssertionError(f"{a}: read succeeded through a chain ending nowhere")
                allowed(os.symlink, os.path.abspath(a), c)               # and closed again

                # The tightest ring there is: a link naming itself. One hop, not forty.
                itself = os.path.join(ring, "itself")
                allowed(os.symlink, os.path.abspath(itself), itself)
                try:
                    os.stat(itself)
                except OSError as e:
                    if e.errno not in refusals:
                        raise AssertionError(f"{itself}: stat on a self-naming link gave "
                                             f"{errno.errorcode.get(e.errno, e.errno)}") from e
                else:
                    raise AssertionError(f"{itself}: stat SUCCEEDED on a link that names itself")

                # two directories each linking to the other: a non-following walk terminates; a following one
                # is bounded here, since the point is that every step is answered
                one, two = os.path.join(ring, "d1"), os.path.join(ring, "d2")
                os.makedirs(one, exist_ok=True)
                os.makedirs(two, exist_ok=True)
                allowed(os.symlink, os.path.abspath(two), os.path.join(one, "down"))
                allowed(os.symlink, os.path.abspath(one), os.path.join(two, "down"))
                seen = 0
                for _, subdirs, files in os.walk(one, followlinks=False):
                    seen += len(subdirs) + len(files)
                    if seen > CYCLE_WALK_CEILING:
                        raise AssertionError(f"{one}: walking two directories that link to each other passed "
                                             f"{CYCLE_WALK_CEILING} entries without following anything")
                laps = 0
                for _, _, _ in os.walk(one, followlinks=True):
                    laps += 1
                    if laps > CYCLE_WALK_CEILING:
                        break                                   # it goes round for ever, and answered every step

            elif variant == "hardlink":
                # two names, one inode. Refusing is allowed; silently making a copy is not
                first, second = fresh("link-first") + ".bin", fresh("link-second") + ".bin"
                created_roots += [first, second]
                body = payload(b"hardlink", 8192)
                with open(first, "wb") as fh:
                    fh.write(body)
                allowed(os.link, first, second)

                if contents(second) != body:
                    raise AssertionError(f"{second}: the second name does not read the first's bytes")
                one, other = os.stat(first), os.stat(second)
                if (one.st_ino, one.st_dev) != (other.st_ino, other.st_dev):
                    raise AssertionError(f"{second}: a different inode from {first} — that is a COPY, not a link")
                if one.st_nlink < 2:
                    raise AssertionError(f"{first}: st_nlink is {one.st_nlink} with two names pointing here")

                extra = payload(b"hardlink-more", 1024)
                with open(first, "ab") as fh:
                    fh.write(extra)
                if contents(second) != body + extra:
                    raise AssertionError(f"{second}: a write through {first} was not visible through the link")
                os.unlink(first)
                created_roots.remove(first)
                if contents(second) != body + extra:
                    raise AssertionError(f"{second}: unlinking the other name took the data with it")

            elif variant == "populated_subtree_moves":
                # a rename re-prefixes a whole subtree; move it into another directory, back out, and out of
                # the phase's directory, checking the manifest at every address
                yard = fresh("yard")
                created_roots.append(yard)
                os.makedirs(yard, exist_ok=True)
                tree = os.path.join(yard, "movable")
                planted = plant_subtree(tree)
                check_subtree(tree, planted)

                # ... in, two levels down into a sibling
                nest = os.path.join(yard, "nest", "deeper")
                os.makedirs(nest, exist_ok=True)
                inside = os.path.join(nest, "movable")
                allowed(os.rename, tree, inside)
                check_subtree(inside, planted)

                # ... and back out, which is the same move with the prefixes the other way round
                allowed(os.rename, inside, tree)
                check_subtree(tree, planted)

                # and up out of the phase's own directory: two levels of prefix gone in one rename
                escaped = os.path.join(directory, f"w{writer}-escaped-{seq}")
                created_roots.append(escaped)
                allowed(os.rename, tree, escaped)
                check_subtree(escaped, planted)

                # ... and back down again, so the tree ends where it started having been four addresses
                allowed(os.rename, escaped, tree)
                created_roots.remove(escaped)
                check_subtree(tree, planted)

            elif variant == "alphabet_path":
                # a/b/c/.../z: deep in components, short in characters
                alpha = fresh("alphabet")
                created_roots.append(alpha)
                here = alpha
                for letter in ALPHABET:
                    here = os.path.join(here, letter)
                os.makedirs(here, exist_ok=True)
                leaf = os.path.join(here, "omega.bin")
                body = payload(b"alphabet", 8192)
                write_whole(leaf, body)
                # a link at the bottom naming the top
                allowed(os.symlink, os.path.abspath(alpha), os.path.join(here, "back-to-the-start"))
                if contents(leaf) != body:
                    raise AssertionError(f"{leaf}: twenty-six levels down did not read back")

                # move the middle ('m' and below) to the top in one rename
                middle = os.path.join(alpha, *ALPHABET[:13])
                moved = os.path.join(alpha, "m-moved")
                allowed(os.rename, middle, moved)
                landed = os.path.join(moved, *ALPHABET[13:], "omega.bin")
                if contents(landed) != body:
                    raise AssertionError(f"{landed}: moving thirteen levels of tree by one name lost the leaf")
                back = os.path.join(moved, *ALPHABET[13:], "back-to-the-start")
                if not stat.S_ISLNK(os.lstat(back).st_mode):
                    raise AssertionError(f"{back}: the link at the bottom did not survive the move as a link")

            elif variant == "rename_into_own_subtree":
                # `mv a a/b/c` must fail with EINVAL; a server that allows it detaches the tree, unreachable
                # but still on disk. Each refusal below is checked to have changed nothing.
                plot = fresh("plot")
                created_roots.append(plot)
                os.makedirs(plot, exist_ok=True)
                tree = os.path.join(plot, "tree")
                planted = plant_subtree(tree)
                loose = os.path.join(plot, "loose.bin")
                loose_body = payload(b"loose", 4096)
                write_whole(loose, loose_body)
                full = os.path.join(tree, "one", "two")          # a directory of the tree's own, not empty

                for what, act in (
                    ("mv tree into its own child", lambda: os.rename(tree, os.path.join(tree, "one", "swallowed"))),
                    ("mv tree into its own grandchild", lambda: os.rename(tree, os.path.join(full, "swallowed"))),
                    ("mv tree onto its own child", lambda: os.rename(tree, full)),
                    ("rmdir a directory with things in it", lambda: os.rmdir(tree)),
                    ("unlink a directory", lambda: os.unlink(tree)),
                    ("rmdir a file", lambda: os.rmdir(loose)),
                    ("mkdir over a file", lambda: os.mkdir(loose)),
                    ("open a directory for writing", lambda: open(tree, "wb").close()),
                    ("truncate a directory", lambda: os.truncate(tree, 0)),
                    ("mv a file onto a full directory", lambda: os.rename(loose, full)),
                    ("mv a directory onto a file", lambda: os.rename(full, loose)),
                    ("mv a directory into a file", lambda: os.rename(full, os.path.join(loose, "under-a-file"))),
                ):
                    try:
                        act()
                    except OSError as e:
                        if e.errno in (errno.ENOENT, errno.ESTALE):
                            raise AssertionError(
                                f"`{what}` was refused with {errno.errorcode.get(e.errno, e.errno)} — which "
                                f"says the thing is NOT THERE, and it is") from e
                    else:
                        raise AssertionError(f"`{what}` SUCCEEDED — the tree is now unreachable from its root")

                # and nothing any of that touched has moved
                check_subtree(tree, planted)
                if contents(loose) != loose_body:
                    raise AssertionError(f"{loose}: a file beside twelve refused operations changed anyway")
                if not os.path.isdir(full):
                    raise AssertionError(f"{full}: a refused rename took the directory with it")

                # renames that must work, so the refusals above are not a blanket no
                sideways = os.path.join(plot, "tree-moved")
                allowed(os.rename, tree, sideways)
                check_subtree(sideways, planted)
                empty_target = os.path.join(plot, "empty-target")
                os.makedirs(empty_target, exist_ok=True)
                allowed(os.rename, sideways, empty_target)       # onto an EMPTY directory: legal, replaces it
                check_subtree(empty_target, planted)

            elif variant == "hostile_names":
                # every byte but NUL and '/' is legal in a name and must not be normalised, escaped, trimmed
                # or folded. One name at a time, so a failure means mangled, not collided.
                yard = fresh("names")
                created_roots.append(yard)
                os.makedirs(yard, exist_ok=True)
                # only the name past MAX_NAME_LENGTH may be refused; refusing any other is a finding
                names = hostile_names()
                refused_names: list[str] = []
                wrong_errno: list[str] = []
                carried = 0
                for index, hostile in enumerate(names):
                    body = payload(f"hostile{index}".encode(), 512)
                    path = os.path.join(yard, hostile)
                    over = len(hostile.encode()) > MAX_NAME_LENGTH
                    try:
                        write_whole(path, body)
                    except OSError as e:
                        code = errno.errorcode.get(e.errno, e.errno)
                        if over and e.errno == errno.ENAMETOOLONG:
                            continue
                        if over:
                            wrong_errno.append(f"{len(hostile.encode())} bytes ({code})")
                            continue
                        # collected, since the set of failing names shows which characters are the problem
                        refused_names.append(f"{hostile!r} ({code})")
                        continue

                    listed = os.listdir(yard)
                    if hostile not in listed:
                        near = [n for n in listed if n.strip() == hostile.strip()]
                        raise AssertionError(
                            f"{hostile!r}: written, and the listing calls it {near or listed!r} — a name that "
                            f"comes back changed is a file stored under somebody else's name")
                    if contents(path) != body:
                        raise AssertionError(f"{hostile!r}: does not read back what was written to it")
                    if not stat.S_ISREG(os.lstat(path).st_mode):
                        raise AssertionError(f"{hostile!r}: is not a regular file")

                    # and it survives being renamed to another awkward name and back, within the cap
                    padded = f" .{hostile}. "
                    if len(padded.encode()) > MAX_NAME_LENGTH:
                        os.unlink(path)
                        carried += 1
                        continue
                    other = os.path.join(yard, padded)
                    try:
                        os.rename(path, other)
                    except OSError as e:
                        if e.errno not in REFUSALS and e.errno != errno.ENAMETOOLONG:
                            raise
                    else:
                        if contents(other) != body:
                            raise AssertionError(f"{hostile!r}: renaming it to a padded name changed its bytes")
                        os.rename(other, path)
                    os.unlink(path)
                    carried += 1

                # refusing the names wholesale is a failure, not an exemption
                if refused_names:
                    raise AssertionError(
                        f"{len(refused_names)} of {len(names)} names within the {MAX_NAME_LENGTH}-byte limit "
                        f"were refused: " + ", ".join(refused_names[:6])
                        + (" …" if len(refused_names) > 6 else ""))
                if wrong_errno:
                    raise AssertionError(
                        f"a name past the {MAX_NAME_LENGTH}-byte limit was refused with the wrong error, "
                        f"expected ENAMETOOLONG: " + ", ".join(wrong_errno))
                if carried < len(names) - 1:
                    raise AssertionError(
                        f"only {carried} of {len(names)} hostile names round-tripped; the rest went "
                        f"untested, so this variant proved nothing about them")

            elif variant == "deep_tree":
                # deeper than one lookup request carries, so the client chains them
                depth, here = 60, base
                top = None
                for level in range(depth):
                    here = os.path.join(here, f"d{level:02d}-{'x' * (level % 7 + 1)}")
                    if top is None:
                        top = here          # the one directory whose removal takes the whole tree
                os.makedirs(here, exist_ok=True)
                leaf = os.path.join(here, "leaf.bin")
                body = payload(b"deep", 4096)
                write_whole(leaf, body)
                if len(leaf) < 300:
                    raise AssertionError(f"the deep-tree path is only {len(leaf)} chars — too shallow to be a test")
                ok = contents(leaf) == body
                shutil.rmtree(top, ignore_errors=True)
                if not ok:
                    raise AssertionError(f"a file {depth} directories down did not read back ({len(leaf)}-char path)")

            elif variant == "deep_rename_above":
                # a handle stays open on a leaf while a directory twelve levels up is renamed, and writing
                # continues through it
                root = fresh("tall")
                created_roots.append(root)      # removed in the variant's finally, refused rename or not
                here = root
                for level in range(12):
                    here = os.path.join(here, f"lvl{level}")
                os.makedirs(here, exist_ok=True)
                leaf = os.path.join(here, "held.bin")
                first = payload(b"before-the-rename", 2048)
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0x1A4)
                try:
                    write_all(fd, first)
                    moved_root = root + "-moved"
                    allowed(os.rename, root, moved_root)
                    second = payload(b"after-the-rename", 2048)
                    write_all(fd, second)               # through the handle, whose path just moved
                    os.fsync(fd)
                finally:
                    os.close(fd)
                landed = os.path.join(moved_root, *[f"lvl{i}" for i in range(12)], "held.bin")
                got = contents(landed)
                created_roots.append(moved_root)
                if got != first + second:
                    raise AssertionError(
                        f"a handle held across a rename twelve levels above it landed {len(got)} bytes, "
                        f"expected {len(first + second)} — the handle's path was not re-prefixed correctly")

            elif variant == "wide_directory":
                # spans many directory-read windows; an entry that does not fit must end the read, not the listing
                d = fresh("wide")
                os.makedirs(d, exist_ok=True)
                # 512 per writer: many windows, without piling up thousands of entries across repeats
                wanted = set()
                for i in range(512):
                    # names of wildly different lengths, so entries straddle the window boundary
                    name = f"e{i:05d}-" + "n" * (1 + (i * 37) % 180)
                    with open(os.path.join(d, name), "wb") as fh:
                        fh.write(b"w")
                    wanted.add(name)
                listed = not_sidecars(os.listdir(d))
                extra = listed - wanted
                lost = wanted - listed
                # removed before asserting, so a failure does not leave the entries behind for later phases
                shutil.rmtree(d, ignore_errors=True)
                if lost or extra:
                    raise AssertionError(
                        f"a {len(wanted)}-entry directory listed {len(listed)} names — {len(lost)} missing "
                        f"(e.g. {sorted(lost)[:2]}), {len(extra)} unexpected")

            elif variant == "extended_attributes":
                # extended attributes may be refused, but an accepted one must come back exactly; a store
                # that drops them silently loses Finder tags, quarantine flags, SELinux labels and capabilities
                if not XATTR_SUPPORTED:
                    result.refused[variant] = f"{sys.platform} offers no extended-attribute calls at all"
                    continue

                f = fresh("xattr") + ".bin"
                created_roots.append(f)
                body = payload(b"xattr", 1024)
                write_whole(f, body)

                # user.* is writable unprivileged on Linux; macOS has no namespace requirement
                key, value = "user.fshammer.probe", b"a value with \x00 a NUL and \xff a high byte"
                try:
                    xattr_set(f, key, value)
                except OSError as exc:
                    if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EACCES, errno.ENODATA):
                        result.refused[variant] = (
                            f"extended attributes are refused ({errno.errorcode.get(exc.errno, exc.errno)}), "
                            f"which is an honest answer callers can act on")
                        os.unlink(f)
                        continue
                    raise

                got = xattr_get(f, key)
                if got != value:
                    raise AssertionError(
                        f"{f}: setxattr({key!r}) was ACCEPTED and getxattr returns {got!r} rather than "
                        f"{value!r} — an attribute that comes back changed is metadata silently corrupted, "
                        f"and every backup that copies it will copy the wrong thing")

                if key not in xattr_list(f):
                    raise AssertionError(
                        f"{f}: {key!r} was set and reads back, and listxattr does not name it — a backup "
                        f"enumerates before it copies, so this attribute will not be preserved")

                # and must survive a rename, which is how atomic-replace publishes a file
                moved = f + ".moved"
                os.rename(f, moved)
                created_roots.append(moved)
                after = xattr_get(moved, key)
                if after != value:
                    raise AssertionError(
                        f"{moved}: the attribute did not survive a rename ({after!r}) — every publish that "
                        f"renames a staged file into place loses it at the moment of going live")

                xattr_remove(moved, key)
                if key in xattr_list(moved):
                    raise AssertionError(f"{moved}: removexattr returned and {key!r} is still listed")
                if contents(moved) != body:
                    raise AssertionError(f"{moved}: the attribute round trip cost the file's contents")
                os.unlink(moved)

            elif variant == "normalisation_is_consistent":
                # café as NFC and NFD. Keeping them apart (ext4) or folding them (HFS+) are both fine; a
                # directory listing two names that resolve to one file is not.
                d = fresh("normalisation")
                created_roots.append(d)
                os.makedirs(d, exist_ok=True)
                composed, decomposed = "caf\u00e9.bin", "cafe\u0301.bin"
                first, second = payload(b"nfc", 512), payload(b"nfd", 512)

                write_whole(os.path.join(d, composed), first)
                write_whole(os.path.join(d, decomposed), second)
                listed = not_sidecars(os.listdir(d))

                if len(listed) == 1:
                    # folded: one file, holding the bytes written last
                    survivor = os.path.join(d, next(iter(listed)))
                    if contents(survivor) != second:
                        raise AssertionError(
                            f"{d}: the two Unicode spellings of one name fold together, and the survivor holds "
                            f"the FIRST write rather than the second — the later write was acknowledged and "
                            f"then discarded")
                    # and both spellings must reach it
                    for spelling in (composed, decomposed):
                        if contents(os.path.join(d, spelling)) != second:
                            raise AssertionError(
                                f"{d}: names fold on write but {spelling!r} does not resolve on read — a name "
                                f"that is taken and cannot be opened is a file nobody can ever get back")
                elif len(listed) == 2:
                    # distinct: each must hold its own bytes
                    for spelling, want in ((composed, first), (decomposed, second)):
                        got = contents(os.path.join(d, spelling))
                        if got != want:
                            raise AssertionError(
                                f"{d}: {spelling!r} is listed as its own entry but reads back the OTHER "
                                f"spelling's bytes — two names resolving to one file, with the listing "
                                f"insisting there are two")
                else:
                    raise AssertionError(
                        f"{d}: two names were written and the directory lists {sorted(listed)!r}")

            elif variant == "case_is_consistent":
                # the same for case: one file or two are both fine, but two entries sharing one file is not
                d = fresh("casefold")
                created_roots.append(d)
                os.makedirs(d, exist_ok=True)
                lower, upper = "photo.bin", "PHOTO.bin"
                first, second = payload(b"lower", 512), payload(b"upper", 512)

                write_whole(os.path.join(d, lower), first)
                write_whole(os.path.join(d, upper), second)
                listed = not_sidecars(os.listdir(d))

                if len(listed) == 1:
                    for spelling in (lower, upper):
                        if contents(os.path.join(d, spelling)) != second:
                            raise AssertionError(
                                f"{d}: names fold by case, so both spellings must reach the file the LAST "
                                f"write left — {spelling!r} does not")
                elif len(listed) == 2:
                    for spelling, want in ((lower, first), (upper, second)):
                        if contents(os.path.join(d, spelling)) != want:
                            raise AssertionError(
                                f"{d}: {spelling!r} is its own entry and reads back the other one's bytes — "
                                f"two names, one file, and the first write is gone")
                else:
                    raise AssertionError(f"{d}: two names were written and the directory lists {sorted(listed)!r}")

            elif variant == "path_is_not_truncated":
                # a long path, built from short components so no single name is near NAME_MAX. Too long must
                # be refused; a stack that truncates the path writes to a different file and reads it back
                d = fresh("longpath")
                created_roots.append(d)
                here = d
                os.makedirs(here, exist_ok=True)
                depth = 0
                try:
                    while len(here) < 3000 and depth < 120:
                        here = os.path.join(here, "component-of-a-fairly-long-name-%03d" % depth)
                        os.mkdir(here)
                        depth += 1
                except OSError as exc:
                    if exc.errno not in (errno.ENAMETOOLONG, errno.ENOSPC):
                        raise
                    # An honest limit, honestly reported. What matters is what was built before it.

                if depth == 0:
                    raise AssertionError(f"{d}: could not create even one subdirectory")

                leaf = os.path.join(here, "leaf.bin")
                body = payload(b"deeppath", 1024)
                try:
                    write_whole(leaf, body)
                except OSError as exc:
                    if exc.errno != errno.ENAMETOOLONG:
                        raise
                    result.refused[variant] = f"paths beyond {len(here)} characters are refused"
                else:
                    if contents(leaf) != body:
                        raise AssertionError(
                            f"a file {len(leaf)} characters down does not read back — the path was accepted "
                            f"and the bytes went somewhere else")
                    # the file must be at the full path; a truncated path would pass the read above too
                    if not os.path.isfile(leaf):
                        raise AssertionError(f"{leaf}: written and read back, and stat cannot find it")
                    if os.path.basename(here) not in os.listdir(os.path.dirname(here)):
                        raise AssertionError(
                            f"the deepest directory is missing from its own parent's listing at depth {depth} "
                            f"— the path was truncated somewhere and the tree is not the shape it reports")

            elif variant == "long_names":
                # Individual components close to the limit, which is a different constraint from a long path.
                d = fresh("longnames")
                os.makedirs(d, exist_ok=True)
                for length in name_lengths():
                    name = ("L%03d-" % length).ljust(length, "z")[:length]
                    path = os.path.join(d, name)
                    body = payload(b"long", 256)
                    try:
                        write_whole(path, body)
                    except OSError as exc:
                        if exc.errno == errno.ENAMETOOLONG:
                            continue           # a real limit, honestly reported
                        raise
                    if contents(path) != body:
                        raise AssertionError(f"a file with a {length}-character name did not read back")
                    if name not in os.listdir(d):
                        raise AssertionError(f"a {length}-character name is missing from its own directory")
                shutil.rmtree(d, ignore_errors=True)

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)
        finally:
            for made in created_roots:
                if os.path.isdir(made):
                    shutil.rmtree(made, ignore_errors=True)
                else:
                    try:
                        os.unlink(made)
                    except OSError:
                        pass

    return result


CONTEST_ROUNDS = 24
CONTEST_BODY = 512


def hammer_contest(directory: str, writer: int, deadline: float,
                   rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Two claims on one name back to back; only the first may win.

    `mkdir x` then `open(x, O_CREAT|O_EXCL)`: the second must fail with EEXIST. A write-behind store that
    acknowledges before applying can get this wrong three ways: both succeed, the wrong one wins, or a delete
    applied late removes what replaced it (`rmdir x` then `open(x, O_CREAT)`). Repeated on fresh names,
    since each is a race against the gap between acknowledging and applying.
    """
    result = PhaseResult.over(("dir_then_file", "file_then_dir", "rmdir_then_file",
                               "unlink_then_dir", "flip_flop_settles"))
    base = os.path.join(directory, f"w{writer}-contest")
    os.makedirs(base, exist_ok=True)
    seq = 0

    def claimed_twice(variant: str, name: str, first: str, second: str) -> None:
        raise AssertionError(
            f"{name}: {first} and {second} BOTH succeeded on one name — a name is a single slot, so the "
            f"second claim was validated against a store that had not yet applied the first ({variant})")

    def expect_refused(variant: str, name: str, first: str, second: str, call) -> None:
        """The second claim must lose, and must lose with a REASON rather than by accident."""
        try:
            call()
        except FileExistsError:
            return                                  # EEXIST — the answer a local filesystem gives
        except IsADirectoryError:
            return                                  # EISDIR — equally honest for a file create over a directory
        except NotADirectoryError:
            return                                  # ENOTDIR — likewise for a mkdir under a file
        except OSError as exc:
            raise AssertionError(
                f"{name}: {second} after {first} failed with {errno.errorcode.get(exc.errno, exc.errno)} "
                f"rather than EEXIST — the name IS taken, and a caller told something else will not retry "
                f"correctly ({variant})") from exc
        claimed_twice(variant, name, first, second)

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "contest"):
        seq += 1
        made: list[str] = []
        try:
            for round_number in range(CONTEST_ROUNDS):
                name = os.path.join(base, f"{variant}-{seq}-{round_number}")
                made.append(name)
                body = payload(f"{writer}:{seq}:{round_number}".encode(), CONTEST_BODY)

                if variant == "dir_then_file":
                    os.mkdir(name)
                    expect_refused(variant, name, "mkdir", "create", lambda: os.close(
                        os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)))
                    if not os.path.isdir(name):
                        raise AssertionError(f"{name}: mkdir won and the name is not a directory")

                elif variant == "file_then_dir":
                    write_whole(name, body)
                    expect_refused(variant, name, "create", "mkdir", lambda: os.mkdir(name))
                    if not os.path.isfile(name):
                        raise AssertionError(f"{name}: the create won and the name is not a file")
                    if contents(name) != body:
                        raise AssertionError(f"{name}: the create won and the bytes are not the ones it wrote")

                elif variant == "rmdir_then_file":
                    # both calls succeed; only what the name holds afterwards shows a late delete
                    os.mkdir(name)
                    os.rmdir(name)
                    write_whole(name, body)
                    if not os.path.isfile(name):
                        raise AssertionError(
                            f"{name}: rmdir then create, both reported success, and the name is not a file — "
                            "the delete was applied AFTER the create and took it")
                    if contents(name) != body:
                        raise AssertionError(f"{name}: recreated after rmdir and the bytes are not the ones written")

                elif variant == "unlink_then_dir":
                    write_whole(name, body)
                    os.unlink(name)
                    os.mkdir(name)
                    if not os.path.isdir(name):
                        raise AssertionError(
                            f"{name}: unlink then mkdir, both reported success, and the name is not a "
                            "directory — the delete was applied AFTER the mkdir and took it")

                else:   # flip_flop_settles
                    # many alternations, then the name must hold what the last call made
                    for flip in range(6):
                        if flip % 2 == 0:
                            os.mkdir(name)
                            os.rmdir(name)
                        else:
                            write_whole(name, body)
                            os.unlink(name)
                    write_whole(name, body)
                    if not os.path.isfile(name):
                        raise AssertionError(
                            f"{name}: after alternating directory and file six times the final create did not "
                            "settle — the operations were not applied in the order they were made")
                    if contents(name) != body:
                        raise AssertionError(f"{name}: the final create settled with the wrong bytes")

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)
        finally:
            for name in made:
                if os.path.isdir(name):
                    shutil.rmtree(name, ignore_errors=True)
                else:
                    try:
                        os.unlink(name)
                    except OSError:
                        pass

    return result


def hammer_identity(directory: str, writer: int, deadline: float,
                    rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Change a file's content while its (size, mtime) stay the same.

    Caches keyed on that pair (version fields, ETags like W/"{length}-{mtime}") then serve stale bytes. It
    happens on its own: tar -x, rsync -t, cp -p and unzip restore timestamps, and fixed-width record files
    are rewritten in place. Reads go through contents() to bypass the page cache where possible.
    """
    result = PhaseResult.over(("same_size_swap", "warm_then_swap", "frozen_series", "shared_mtime_collision",
                               "mtime_roundtrip", "truncate_to_same_size", "rename_over_same_size",
                               "last_byte_only", "through_zero", "directory_mtime", "sentinel_mtimes",
                               "interleaved_pair", "chmod_keeps_bytes_and_mtime", "truncate_grow_reads_zeros",
                               "utimes_survives_the_flush", "mtime_is_not_in_the_future",
                               "ctime_is_not_just_mtime", "size_is_where_the_file_ends"))
    base = os.path.join(directory, f"w{writer}-identity")
    os.makedirs(base, exist_ok=True)

    def mtime_of(path: str) -> int:
        # whole seconds: not every protocol carries finer mtimes
        return int(os.stat(path).st_mtime)

    def restore(path: str, when: int) -> None:
        os.utime(path, (when, when))

    def expect(variant: str, path: str, want: bytes, when: "int | None") -> None:
        got = contents(path)
        if got != want:
            raise AssertionError(
                f"{path}: read back {len(got)} bytes starting {got[:32]!r}, but {len(want)} bytes starting "
                f"{want[:32]!r} were written. This variant keeps size and mtime unchanged across the "
                f"rewrite, so a cache keyed on them would return the old content")
        if when is not None:
            now = mtime_of(path)
            if now != when:
                raise AssertionError(f"{path}: mtime reads {now} but {when} was set ({now - when:+d}s)")

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "identity"):
        path = os.path.join(base, f"{variant}.bin")
        size = rng.choice([5, 512, 4096, 32768, 40960])
        try:
            if variant == "same_size_swap":
                first, second = payload(b"hello", size), payload(b"world", size)
                write_whole(path, first)
                when = mtime_of(path)
                write_whole(path, second)          # SAME LENGTH, different bytes
                restore(path, when)
                expect(variant, path, second, when)

            elif variant == "warm_then_swap":
                # The dangerous order: read everything into every cache first, then swap underneath it.
                first, second = payload(b"warm", size), payload(b"cold", size)
                write_whole(path, first)
                when = mtime_of(path)
                expect(variant, path, first, when)  # populate
                write_whole(path, second)
                restore(path, when)
                expect(variant, path, second, when)

            elif variant == "frozen_series":
                # one timestamp, many contents
                when = None
                for i in range(rng.randint(3, 8)):
                    data = payload(f"gen{i}".encode(), size)
                    write_whole(path, data)
                    if when is None:
                        when = mtime_of(path)
                    restore(path, when)
                    expect(variant, path, data, when)

            elif variant == "shared_mtime_collision":
                # Several files, one timestamp, one size, different bytes. Each must still be itself.
                when = None
                group = [(os.path.join(base, f"collide-{i}.bin"), payload(f"c{i}".encode(), size))
                         for i in range(rng.randint(2, 5))]
                for p2, data in group:
                    write_whole(p2, data)
                    if when is None:
                        when = mtime_of(p2)
                    restore(p2, when)
                for p2, data in group:
                    expect(variant, p2, data, when)

            elif variant == "mtime_roundtrip":
                data = payload(b"round", size)
                write_whole(path, data)
                when = mtime_of(path)
                restore(path, when + 100000)        # forward
                restore(path, when)                 # and back
                expect(variant, path, data, when)

            elif variant == "truncate_to_same_size":
                data = payload(b"trunc", size)
                write_whole(path, data)
                when = mtime_of(path)
                fd = os.open(path, os.O_WRONLY)
                try:
                    os.ftruncate(fd, size)          # a no-op truncate, which must not lose the bytes
                finally:
                    os.close(fd)
                restore(path, when)
                expect(variant, path, data, when)

            elif variant == "last_byte_only":
                # differs only in the last byte, for caches that fingerprint a prefix
                first = payload(b"tail", size)
                second = first[:-1] + bytes([first[-1] ^ 0xFF])
                write_whole(path, first)
                when = mtime_of(path)
                write_whole(path, second)
                restore(path, when)
                expect(variant, path, second, when)

            elif variant == "through_zero":
                # same size and mtime at the end, but through length 0 on the way
                first, second = payload(b"before-zero", size), payload(b"after-zero", size)
                write_whole(path, first)
                when = mtime_of(path)
                fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
                os.close(fd)
                write_whole(path, second)
                restore(path, when)
                expect(variant, path, second, when)

            elif variant == "directory_mtime":
                # a directory listing cached against the directory's mtime, which is then put back
                sub = os.path.join(base, "dir-mtime")
                os.makedirs(sub, exist_ok=True)
                stable = os.path.join(sub, "stays.bin")
                write_whole(stable, payload(b"stays", 16))
                when = mtime_of(sub)
                added = os.path.join(sub, f"added-{rng.randrange(1 << 30)}.bin")
                write_whole(added, payload(b"added", 16))
                restore(sub, when)
                names = set(os.listdir(sub))
                if os.path.basename(added) not in names:
                    raise AssertionError(
                        f"{sub}: listing omits {os.path.basename(added)} after the directory mtime was put "
                        f"back — a cached listing answered instead of the directory")
                os.unlink(added)
                restore(sub, when)
                if os.path.basename(added) in set(os.listdir(sub)):
                    raise AssertionError(f"{sub}: listing still shows {os.path.basename(added)} after unlink")

            elif variant == "sentinel_mtimes":
                # 0 and a far-future mtime, which code often treats as unset; they must not be rounded or clamped
                data = payload(b"sentinel", size)
                write_whole(path, data)
                # if both are refused nothing was verified, so it does not count as completed
                asked = 0
                for when in (0, 2 ** 31 - 1):
                    try:
                        restore(path, when)
                    except OSError:
                        continue        # a backend may refuse the range outright, which is honest
                    expect(variant, path, data, when)
                    asked += 1
                if not asked:
                    raise Refused("utime refused both sentinel timestamps, so nothing was verified")

            elif variant == "interleaved_pair":
                # two files swap contents with both timestamps frozen
                a, b = os.path.join(base, "pair-a.bin"), os.path.join(base, "pair-b.bin")
                da, db = payload(b"aaa", size), payload(b"bbb", size)
                write_whole(a, da)
                write_whole(b, db)
                when = mtime_of(a)
                restore(b, when)
                write_whole(a, db)      # a now holds b's bytes
                write_whole(b, da)      # and b holds a's
                restore(a, when)
                restore(b, when)
                expect(variant, a, db, when)
                expect(variant, b, da, when)

            elif variant == "chmod_keeps_bytes_and_mtime":
                # SETATTR carries mode and times together; a chmod must not move mtime or touch the data
                body = payload(variant.encode(), size)
                write_whole(path, body)
                when = mtime_of(path)
                before = os.stat(path)
                for mode in (0o600, 0o644, 0o640, 0o664):
                    os.chmod(path, mode)
                    now = os.stat(path)
                    if stat.S_IMODE(now.st_mode) != mode:
                        raise AssertionError(f"{path}: chmod {mode:o} reads back as {stat.S_IMODE(now.st_mode):o}")
                    if int(now.st_mtime) != when:
                        raise AssertionError(f"{path}: chmod {mode:o} moved mtime to {int(now.st_mtime)} "
                                             f"from {when} ({int(now.st_mtime) - when:+d}s)")
                    if now.st_size != before.st_size:
                        raise AssertionError(f"{path}: chmod {mode:o} changed the size to {now.st_size}")
                expect(variant, path, body, when)

            elif variant == "truncate_grow_reads_zeros":
                # growing by truncate makes a hole, which must read as zeros, for its full length
                head = payload(variant.encode(), size)
                write_whole(path, head)
                grown = size + 40960
                os.truncate(path, grown)
                got = contents(path)
                if len(got) != grown:
                    raise AssertionError(f"{path}: truncate to {grown} left {len(got)} bytes "
                                         f"({len(got) - grown:+d})")
                if got[:size] != head:
                    raise AssertionError(f"{path}: growing the file changed bytes that were already there")
                if got[size:] != bytes(grown - size):
                    raise AssertionError(f"{path}: the hole after {size} is not zeros — {got[size:size + 32]!r}")

                os.truncate(path, size)
                if contents(path) != head:
                    raise AssertionError(f"{path}: shrinking back to {size} did not leave the original bytes")
                expect(variant, path, head, None)

            elif variant == "utimes_survives_the_flush":
                # cp -p, rsync -t, tar -x and unzip write, set mtime, then close. If the close flushes writes,
                # the server stamps mtime after the SETATTR and the requested time is lost ("Why NFS Sucks",
                # Kirch). The time must match exactly.
                body = payload(variant.encode(), size)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                when = 0
                try:
                    write_all(fd, body)                 # dirty pages outstanding, deliberately not flushed
                    when = int(time.time()) - 86400
                    os.utime(path, (when, when))
                finally:
                    os.close(fd)                        # the flush that would clobber it
                after = mtime_of(path)
                if after != when:
                    raise AssertionError(
                        f"{path}: mtime was set to {when} while {len(body)} bytes were still unflushed, and "
                        f"reads {after} after the close ({after - when:+d}s) — the flush stamped the file "
                        f"AFTER the timestamp was set, so cp -p and rsync -t lose the date they restore")
                expect(variant, path, body, when)

            elif variant == "mtime_is_not_in_the_future":
                # mtime rounded up puts files in the future: make thinks targets are built, rsync -u skips
                # updates. Bounded above by the clock after the write (plus a second for coarse
                # granularity) and below to catch an epoch or otherwise absurd stamp.
                before_write = int(time.time())
                write_whole(path, payload(b"clock", size))
                after_write = int(time.time())
                stamped = mtime_of(path)
                if stamped > after_write + 1:
                    raise AssertionError(
                        f"{path}: mtime is {stamped}, which is {stamped - after_write}s AFTER the clock read "
                        f"once the write returned — a file dated in the future makes `make` treat its target "
                        f"as already built and rsync -u refuse to update from it")
                if stamped < before_write - 86400:
                    raise AssertionError(
                        f"{path}: mtime is {stamped}, {before_write - stamped}s BEFORE the write began — a "
                        f"file that old is one every incremental tool will rebuild forever")

            elif variant == "ctime_is_not_just_mtime":
                # ctime must not be an alias of mtime: Linux's NFS client invalidates its cache from ctime, so
                # under aliasing a chmod is invisible. A write and a chmod can share a ctime on a coarse clock,
                # so instead mtime is set a day back explicitly; ctime cannot then equal it without aliasing.
                body = payload(b"ctime", size)
                write_whole(path, body)
                a_day_ago = int(time.time()) - 86400
                os.utime(path, (a_day_ago, a_day_ago))

                first = os.stat(path)
                flipped = 0o600 if stat.S_IMODE(first.st_mode) != 0o600 else 0o640
                allowed(os.chmod, path, flipped)
                second = os.stat(path)

                if second.st_mtime_ns != first.st_mtime_ns:
                    raise AssertionError(
                        f"{path}: chmod moved MTIME as well ({first.st_mtime_ns} -> {second.st_mtime_ns}) — a "
                        f"permission change is not a modification, and every backup keyed on mtime will copy "
                        f"the file again for it")
                if second.st_ctime_ns < first.st_ctime_ns:
                    raise AssertionError(
                        f"{path}: ctime went BACKWARDS across a chmod, {first.st_ctime_ns} -> "
                        f"{second.st_ctime_ns} — nothing downstream can use a clock that runs backwards")
                if second.st_ctime_ns <= second.st_mtime_ns:
                    raise AssertionError(
                        f"{path}: mtime was set to a day ago and ctime reads {second.st_ctime_ns} against an "
                        f"mtime of {second.st_mtime_ns}, so ctime is a copy of mtime rather than a change "
                        f"time of its own. A chmod is then invisible to every client that keys cache "
                        f"invalidation on ctime, which Linux does")
                expect(variant, path, body, a_day_ago)

            elif variant == "size_is_where_the_file_ends":
                # the reported size is where readers think the file ends, so it is checked against how many
                # bytes a read actually returns, for several shapes
                for shape, body in (("empty", b""),
                                    ("one_byte", b"x"),
                                    ("exact", payload(b"exact", size)),
                                    ("grown", payload(b"grown", size))):
                    where = os.path.join(base, f"ends-{shape}.bin")
                    write_whole(where, body)
                    if shape == "grown":
                        # extended by ftruncate, so a store counting only bytes it was handed reports it short
                        fd = os.open(where, os.O_WRONLY)
                        try:
                            os.ftruncate(fd, len(body) + 8192)
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        body = body + bytes(8192)

                    reported = os.stat(where).st_size
                    readable = len(contents(where))
                    if reported != len(body) or readable != len(body):
                        raise AssertionError(
                            f"{where}: {shape} — the file holds {len(body)} bytes, stat reports {reported} "
                            f"and a read returns {readable}. The size is where a client thinks the file "
                            f"ends, so any disagreement is a truncated read or a padded tail")
                    os.unlink(where)

            else:  # rename_over_same_size
                data, replacement = payload(b"before", size), payload(b"after", size)
                write_whole(path, data)
                when = mtime_of(path)
                tmp = path + ".new"
                write_whole(tmp, replacement)
                restore(tmp, when)
                os.rename(tmp, path)                # same size, same mtime, different file entirely
                expect(variant, path, replacement, when)

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except (AssertionError, OSError) as exc:
            result.record(variant, exc)

    return result


def hammer_observer(shared_dir: str, writer: int, deadline: float, rng: random.Random,
                    acked: list[AckedBlock]) -> dict[str, int]:
    """One writer appends a log while every other writer only stats, lists, seeks to the end and reads the
    tail. Observing must not cost the appender a byte.

    A stat racing an append that rewinds the writer's cached i_size is a known client-side failure;
    lseek(SEEK_END) is included because it reads i_size directly. Returns per-kind observation counts; zero
    means the observer never ran, which is reported as a gap.
    """
    path = os.path.join(shared_dir, OBSERVED)
    counts = {k: 0 for k in ("appended",) + OBSERVED_LOOKS}

    # writer 0 appends, the others watch; a second appender would make this the shared phase
    if writer == 0:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        seq, budget = 0, OBSERVED_BUDGET
        give_up = time.monotonic() + OBSERVED_PATIENCE
        try:
            # minimum blocks first, then the clock
            while budget > 0 and time.monotonic() < give_up and (seq < OBSERVED_MIN_BLOCKS or time.monotonic() < deadline):
                length = rng.choice(SHARED_SIZES)
                frame = block(writer, seq, length)
                # ONE write, never a loop: a retry would let a reader see a torn frame it should not
                if os.write(fd, frame) != len(frame):
                    raise AssertionError(f"short write of a {len(frame)}-byte observed block")
                acked.append(AckedBlock(seq, length))
                counts["appended"] += 1
                budget -= len(frame)
                seq += 1
        finally:
            try:
                os.fsync(fd)          # a deferred write-behind error surfaces here and must not skip the close
            finally:
                os.close(fd)
        return counts

    d = os.path.dirname(path)
    give_up = time.monotonic() + OBSERVED_PATIENCE

    # wait for the log to exist; otherwise listdir, the one look that does not touch the file, piles up
    # counts without overlapping any write
    while not os.path.exists(path) and time.monotonic() < give_up:
        time.sleep(0.005)
    if not os.path.exists(path):
        return counts                 # reported as too thin to be evidence, which it is

    # every kind once before random choice, so seek_end always runs
    mandatory = list(OBSERVED_LOOKS)
    while time.monotonic() < give_up and (mandatory or sum(counts.values()) < OBSERVED_MIN_LOOKS
                                          or time.monotonic() < deadline):
        kind = mandatory.pop() if mandatory else rng.choice(OBSERVED_LOOKS)
        try:
            if kind == "stat":
                os.stat(path)
            elif kind == "listdir":
                # entries vanish between listdir and stat in a busy directory; skip them rather than raise.
                # Capped at 64 so a slow mount still gets many observations.
                for entry in os.listdir(d)[:64]:
                    try:
                        os.stat(os.path.join(d, entry))
                    except OSError:
                        continue
            elif kind == "seek_end":
                fh = os.open(path, os.O_RDONLY)
                try:
                    os.lseek(fh, 0, os.SEEK_END)
                finally:
                    os.close(fh)
            else:
                fh = os.open(path, os.O_RDONLY)
                try:
                    end = os.lseek(fh, 0, os.SEEK_END)
                    os.pread(fh, 4096, max(0, end - 4096))
                finally:
                    os.close(fh)
        except (FileNotFoundError, Refused):
            # the file is not there yet; a kind that never succeeded goes back on the list until give_up
            if not counts[kind]:
                mandatory.append(kind)
            continue
        counts[kind] += 1
    return counts


def hammer_malice(directory: str, writer: int, deadline: float,
                  rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Rename, unlink and replace under open handles, and other name/handle cases.

    A failing variant does not stop the others.
    """
    result = PhaseResult.over(("rename_live", "rename_recreate", "rename_over_open", "unlink_open",
                               "torrent", "append_vs_truncate", "dir_rename_open", "many_hot", "negatives",
                               "atomic_replace", "sparse_far_offset", "readdir_under_mutation",
                               "contended_exclusive", "contended_tree", "log_rotation",
                               "overlapping_writers", "mode_is_enforced",
                               "fd_survives_enumeration"))
    seq = 0
    d = os.path.join(directory, f"w{writer}-malice")
    os.makedirs(d, exist_ok=True)

    def name(what: str) -> str:
        return os.path.join(d, f"{what}-{seq}.bin")

    # a reshuffled cycle rather than independent draws, so every variant runs once before any runs twice
    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "malice"):
        seq += 1
        try:
            # ── the handle follows the file, and keeps writing to it under its new name ──
            if variant == "rename_live":
                a, b = name("live"), name("live-moved")
                h1 = os.open(a, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                h2 = os.open(a, os.O_WRONLY | os.O_APPEND)
                try:
                    one = payload(b"live1", rng.choice([1, 4096, 32768]))
                    write_all(h1, one)
                    allowed(os.rename, a, b)                    # same directory: not EXDEV
                    two = payload(b"live2", rng.choice([1, 4096, 32768]))
                    write_all(h1, two)                          # through the fd that outlived the name
                    three = payload(b"live3", rng.choice([1, 512, 32768]))
                    write_all(h2, three)                        # and through the second one
                    sizes_agree(b, h1, "after renaming a file with two live handles", len(one) + len(two) + len(three))
                finally:
                    os.close(h1); os.close(h2)
                got = contents(b)
                want = one + two + three
                if got != want:
                    at = next((i for i in range(min(len(got), len(want))) if got[i] != want[i]), min(len(got), len(want)))
                    raise AssertionError(f"{b}: writes through a renamed file's handles diverge at {at} "
                                         f"(file {len(got)}, expected {len(want)})")
                os.unlink(b)

            # ── a NEW file at the old name must not catch the old handle's writes ──
            elif variant == "rename_recreate":
                a, b = name("recr"), name("recr-moved")
                h = os.open(a, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    keep = payload(b"keep", rng.choice([4096, 32768]))
                    write_all(h, keep)
                    allowed(os.rename, a, b)
                    decoy_fd = os.open(a, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                    try:
                        decoy = payload(b"decoy", rng.choice([1, 4096, 32768]))
                        write_all(decoy_fd, decoy)
                        more = payload(b"more", rng.choice([1, 4096, 32768]))
                        write_all(h, more)                      # the OLD handle, after a new file took its name
                    finally:
                        os.close(decoy_fd)
                finally:
                    os.close(h)
                if contents(b) != keep + more:
                    raise AssertionError(f"{b}: the renamed file did not receive its own handle's writes")
                if contents(a) != decoy:
                    raise AssertionError(f"{a}: a file created at a freed name caught another handle's "
                                         f"writes — {len(contents(a))} bytes, expected {len(decoy)}")
                os.unlink(a); os.unlink(b)

            # ── rename onto an open file: the replaced inode belongs to whoever holds it ──
            elif variant == "rename_over_open":
                a, b = name("over-src"), name("over-dst")
                victim = payload(b"victim", rng.choice([4096, 32768]))
                with open(b, "wb") as fh:
                    fh.write(victim)
                src = payload(b"src", rng.choice([4096, 32768]))
                with open(a, "wb") as fh:
                    fh.write(src)
                hv = os.open(b, os.O_WRONLY | os.O_APPEND)      # holds the file about to be replaced
                try:
                    allowed(os.rename, a, b)                    # silently replaces b
                    ghost = payload(b"ghost", rng.choice([1, 4096]))
                    try:
                        write_all(hv, ghost)                    # into the detached inode, if anywhere
                    except OSError as e:
                        if e.errno not in DESTROYED_REFUSALS:
                            raise
                finally:
                    os.close(hv)
                now = contents(b)
                if now != src:
                    raise AssertionError(f"{b}: after a rename over it the name holds {len(now)} bytes, "
                                         f"expected the source's {len(src)} — a stale handle wrote through a name")
                os.unlink(b)

            # ── unlinked, still open, still writable: the bytes belong to the handle ──
            elif variant == "unlink_open":
                f = name("gone")
                h = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                rh = os.open(f, os.O_RDONLY)
                try:
                    first = payload(b"before", rng.choice([4096, 32768]))
                    write_all(h, first)
                    allowed(os.unlink, f)
                    after = payload(b"after", rng.choice([1, 4096, 32768]))
                    try:
                        write_all(h, after)
                        live = True
                    except OSError as e:
                        if e.errno not in DESTROYED_REFUSALS:
                            raise
                        live, after = False, b""
                    want = first + after
                    if live and os.fstat(h).st_size != len(want):
                        raise AssertionError(f"{f}: after unlink the handle reports "
                                             f"{os.fstat(h).st_size} bytes, expected {len(want)}")
                    try:
                        got = os.pread(rh, len(want) + 1, 0)
                    except OSError as e:
                        if e.errno not in DESTROYED_REFUSALS:
                            raise
                        got = want                              # refusing to read an unlinked file is defensible
                    if got != want:
                        raise AssertionError(f"{f}: an unlinked file's own handle read back {len(got)} bytes, "
                                             f"expected {len(want)}")
                    # and a new file at the freed name must be entirely its own
                    fresh = payload(b"fresh", rng.choice([1, 512, 4096]))
                    with open(f, "wb") as fh:
                        fh.write(fresh)
                    if contents(f) != fresh:
                        raise AssertionError(f"{f}: a file created at an unlinked name is not its own content")
                finally:
                    os.close(h); os.close(rh)
                if contents(f) != fresh:
                    raise AssertionError(f"{f}: closing the unlinked file's handles changed the new file at that name")
                os.unlink(f)

            # ── a torrent client: preallocate, then fill in shuffled pieces from several handles ──
            elif variant == "torrent":
                f = name("torrent")
                span = rng.choice([MALICE_SPAN // 4, MALICE_SPAN])
                model = bytearray(span)
                fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o644)
                hs = []
                try:
                    os.ftruncate(fd, span)
                    hs = [os.open(f, os.O_WRONLY) for _ in range(rng.randint(2, 4))]
                    offsets = list(range(0, span - MALICE_PIECE, MALICE_PIECE))
                    cap = 10 << 20
                    if cap - MALICE_PIECE // 2 < span:
                        offsets.append(cap - MALICE_PIECE // 2)  # one piece straddling the coalescing cap
                    rng.shuffle(offsets)
                    for off in offsets:
                        piece = payload(f"p{off}".encode(), MALICE_PIECE)
                        pwrite_all(rng.choice(hs), piece, off)
                        model[off:off + MALICE_PIECE] = piece
                        if rng.random() < 0.02:
                            sizes_agree(f, fd, "mid-torrent", len(model))
                finally:
                    os.close(fd)
                    for h in hs:
                        os.close(h)
                got = contents(f)
                if got != bytes(model):
                    limit = min(len(got), len(model))
                    at = next((i for i in range(limit) if got[i] != model[i]), limit)
                    raise AssertionError(f"{f}: out-of-order piece writes diverge at offset {at} "
                                         f"(file {len(got)}, expected {len(model)})")
                os.unlink(f)

            # ── an append must land at the real end, not at a remembered one ──
            elif variant == "append_vs_truncate":
                f = name("rewind")
                ha = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                ht = os.open(f, os.O_WRONLY)
                try:
                    before = payload(b"before", rng.choice([4096, 32768, 1 << 20]))
                    write_all(ha, before)
                    os.fstat(ha)                                # cache the size, then pull it out from under
                    os.ftruncate(ht, 0)
                    tail = payload(b"tail", rng.choice([1, 4096, 32768]))
                    write_all(ha, tail)                         # must land at 0, not at len(before)
                    sizes_agree(f, ha, "appending after another handle truncated", len(tail))
                finally:
                    os.close(ha); os.close(ht)
                got = contents(f)
                if got != tail:
                    raise AssertionError(f"{f}: an append after a truncate produced {len(got)} bytes, "
                                         f"expected {len(tail)} — the handle appended at a stale end")
                os.unlink(f)

            # ── rename a directory with a file open and being written inside it ──
            elif variant == "dir_rename_open":
                sub, moved = os.path.join(d, f"sub-{seq}"), os.path.join(d, f"sub-{seq}-moved")
                os.makedirs(sub, exist_ok=True)
                inner = os.path.join(sub, "held.bin")
                h = os.open(inner, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    one = payload(b"dir1", rng.choice([1, 4096, 32768]))
                    write_all(h, one)
                    allowed(os.rename, sub, moved)
                    two = payload(b"dir2", rng.choice([1, 4096, 32768]))
                    write_all(h, two)                           # the handle came along with its directory
                finally:
                    os.close(h)
                landed = os.path.join(moved, "held.bin")
                if contents(landed) != one + two:
                    raise AssertionError(f"{landed}: writes through a handle whose directory was renamed "
                                         f"gave {len(contents(landed))} bytes, expected {len(one + two)}")
                try:
                    os.rmdir(moved)
                    raise AssertionError(f"{moved}: rmdir removed a directory that still holds a file")
                except OSError as e:
                    if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
                os.unlink(landed); os.rmdir(moved)

            # ── many files hot at once, so per-handle state is under simultaneous pressure ──
            elif variant == "many_hot":
                paths = [os.path.join(d, f"hot-{seq}-{i}.bin") for i in range(MALICE_HOT_FILES)]
                models = {p: bytearray() for p in paths}
                hs = {p: os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644) for p in paths}
                try:
                    for _ in range(rng.randint(40, 200)):
                        p = rng.choice(paths)
                        chunk = payload(f"hot{len(models[p])}".encode(), rng.choice([1, 7, 512, 4096, 32768]))
                        write_all(hs[p], chunk)
                        models[p].extend(chunk)
                        if rng.random() < 0.05:
                            sizes_agree(p, hs[p], "one of many hot files", len(models[p]))
                finally:
                    for p, h in hs.items():
                        close_or_explain(h, p, f"being one of {MALICE_HOT_FILES} files open at once")
                for p in paths:
                    if contents(p) != bytes(models[p]):
                        raise AssertionError(f"{p}: one of {MALICE_HOT_FILES} files open at once diverged "
                                             f"({len(contents(p))} bytes, expected {len(models[p])})")
                    os.unlink(p)

            # ── an open descriptor must survive another directory being enumerated ──
            # a log writer while a backup walks the tree. Catches per-handle state sized by something other
            # than what is open; a refused write on an fd cannot be re-sent, so the bytes are lost.
            elif variant == "fd_survives_enumeration":
                wide = os.path.join(d, f"wide-{seq}")
                os.makedirs(wide, exist_ok=True)

                # The held descriptors first, so they are the oldest state the filesystem is keeping.
                held = [os.path.join(d, f"held-{seq}-{i}.bin") for i in range(MALICE_HELD_FDS)]
                models = {q: bytearray() for q in held}
                fds = {q: os.open(q, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644) for q in held}
                born = {q: identity_of(q) for q in held}
                try:
                    for q in held:
                        chunk = payload(b"before", 4096)
                        write_all(fds[q], chunk)
                        models[q].extend(chunk)

                    # far more names than descriptors, and enumerate the parent too, where the held files live
                    for i in range(MALICE_ENUMERATION_NAMES):
                        write_whole(os.path.join(wide, f"e{i:05d}.bin"), b"x")
                    for target in (wide, d):
                        with os.scandir(target) as entries:
                            for _ in entries:
                                pass

                    # And keep writing through the descriptors opened before any of that existed.
                    for q in held:
                        chunk = payload(b"after", 8192)
                        write_all(fds[q], chunk)
                        models[q].extend(chunk)
                        fsync_or_explain(fds[q], q, f"held across an enumeration of "
                                                    f"{MALICE_ENUMERATION_NAMES} names", born.get(q))
                finally:
                    for q, h in fds.items():
                        close_or_explain(h, q, f"being held across an enumeration of "
                                               f"{MALICE_ENUMERATION_NAMES} names", born.get(q))

                for q in held:
                    got = contents(q)
                    if got != bytes(models[q]):
                        raise AssertionError(
                            f"{q}: a descriptor held across an enumeration of "
                            f"{MALICE_ENUMERATION_NAMES} names lost its writes — {len(got)} bytes on the "
                            f"filesystem, {len(models[q])} acknowledged ({len(got) - len(models[q]):+d})")
                    os.unlink(q)
                for i in range(MALICE_ENUMERATION_NAMES):
                    os.unlink(os.path.join(wide, f"e{i:05d}.bin"))
                os.rmdir(wide)

            # ── rename is atomic, and a reader must never catch it half-done ──
            elif variant == "atomic_replace":
                # a reader sees the old file or the new one, never a mix or nothing. Sizes differ too, since a
                # non-atomic replace often shows as new content with the old length
                live = name("atomic")
                old_body, new_body = payload(b"atomic-old", 4096), payload(b"atomic-new", 65536)
                with open(live, "wb") as fh:
                    fh.write(old_body)
                spare = live + ".next"
                for round_no in range(MALICE_REPLACE_ROUNDS):
                    want = new_body if round_no % 2 == 0 else old_body
                    with open(spare, "wb") as fh:
                        fh.write(want)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(spare, live)
                    got = contents(live)
                    if got not in (old_body, new_body):
                        raise AssertionError(
                            f"{live}: a reader saw {len(got)} bytes that are neither whole file "
                            f"({len(old_body)} or {len(new_body)}) — the replace was not atomic")
                    if got != want:
                        raise AssertionError(
                            f"{live}: still reads the previous file after rename(2) returned — "
                            f"{len(got)} bytes where {len(want)} were just put there")
                os.unlink(live)

            # ── an offset past where 32 bits stop counting ──
            elif variant == "sparse_far_offset":
                # past 4 GiB, where 32-bit offsets and sizes wrap. Sparse, so cheap with holes; without holes
                # it needs 4 GiB, so free space is checked first
                usage = os.statvfs(d)
                free = usage.f_bavail * usage.f_frsize
                if free < FAR_OFFSET + (1 << 30):
                    raise Refused(f"{free >> 20} MiB free; a store without holes would need {FAR_OFFSET >> 20} MiB")

                far = name("sparse-far")
                mark = payload(b"far", 512)
                # data at offset 0 first, so the hole is in the middle of a file; both ends must survive
                head = payload(b"head", 512)
                with open(far, "wb") as fh:
                    fh.write(head)
                    fh.seek(FAR_OFFSET)
                    fh.write(mark)
                try:
                    size = os.stat(far).st_size
                    if size != FAR_OFFSET + len(mark):
                        raise AssertionError(f"{far}: size reads {size}, expected {FAR_OFFSET + len(mark)} "
                                             f"({size - FAR_OFFSET - len(mark):+d})")
                    with open(far, "rb") as fh:
                        if fh.read(len(head)) != head:
                            raise AssertionError(f"{far}: the data BELOW the hole did not survive the far write")
                        fh.seek(FAR_OFFSET)
                        got = fh.read(len(mark))
                        if got != mark:
                            raise AssertionError(f"{far}: the bytes at {FAR_OFFSET} came back as {got[:16]!r}")
                        # A hole is defined to read as zeros. Anything else is another file's data.
                        fh.seek(FAR_OFFSET - 4096)
                        hole = fh.read(4096)
                        if hole != bytes(4096):
                            raise AssertionError(f"{far}: the hole below {FAR_OFFSET} is not zeros — {hole[:32]!r}")
                        # and the bytes just after the head, the other edge of the hole
                        fh.seek(len(head))
                        edge = fh.read(4096)
                        if edge != bytes(4096):
                            raise AssertionError(f"{far}: the hole just above the head is not zeros — {edge[:32]!r}")
                finally:
                    os.unlink(far)

            # ── a listing is many calls, and the directory changes between them ──
            elif variant == "readdir_under_mutation":
                # a listing is a sequence of reads resuming from cookies; an entry present for the whole scan
                # must appear exactly once while others come and go
                scanned = os.path.join(d, f"readdir-{seq}")
                os.makedirs(scanned, exist_ok=True)
                try:
                    stable = {f"stay-{i:04d}.bin" for i in range(READDIR_STABLE)}
                    for entry in sorted(stable):
                        with open(os.path.join(scanned, entry), "wb") as fh:
                            fh.write(b"x")
                    churn = [os.path.join(scanned, f"churn-{i:04d}.bin") for i in range(READDIR_CHURN)]
                    for path in churn:
                        with open(path, "wb") as fh:
                            fh.write(b"x")

                    seen: list[str] = []
                    with os.scandir(scanned) as it:
                        for i, entry in enumerate(it):
                            seen.append(entry.name)
                            # mutate while the scan is open; position-derived cookies lose their place
                            if i % 8 == 0 and churn:
                                os.unlink(churn.pop())
                            elif i % 8 == 4:
                                with open(os.path.join(scanned, f"added-{i:04d}.bin"), "wb") as fh:
                                    fh.write(b"x")

                    found = [n for n in seen if n in stable]
                    repeated = sorted({n for n in found if found.count(n) > 1})
                    if repeated:
                        raise AssertionError(f"{scanned}: the scan returned {repeated[:5]} more than once "
                                             f"({len(repeated)} entries) while the directory changed under it")
                    missing = stable - set(found)
                    if missing:
                        raise AssertionError(f"{scanned}: the scan skipped {sorted(missing)[:5]} — "
                                             f"{len(missing)} of {len(stable)} entries that never moved")
                finally:
                    shutil.rmtree(scanned, ignore_errors=True)

            # ── the other writers, on the same names, at the same time ──
            elif variant == "contended_exclusive":
                if procs < 2:
                    result.refused[variant] = "--procs 1, so there was no other writer to contend with"
                    continue
                # O_CREAT|O_EXCL succeeds for exactly one caller, so the winner reads back its own bytes; two
                # winners in a round is a broken mutual exclusion
                contended = os.path.join(directory, "contended.bin")
                mine = payload(f"w{writer}:{seq}".encode(), rng.choice([1, 4096, 32768]))
                # bounded by time, not rounds: losing every round to contention is not a fault
                won = 0
                contended_until = time.monotonic() + 20
                rounds = 0
                while rounds < CONTENDED_ROUNDS or (won == 0 and time.monotonic() < contended_until):
                    rounds += 1
                    try:
                        fd = os.open(contended, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    except FileExistsError:
                        continue                            # another writer holds it this round

                    # unlink in a finally, or a leftover name makes every later round see EEXIST
                    try:
                        try:
                            write_all(fd, mine)
                            os.fsync(fd)
                        finally:
                            os.close(fd)

                        got = contents(contended)
                        if got != mine:
                            raise AssertionError(
                                f"{contended}: created with O_EXCL and holding {len(got)} bytes that are not the "
                                f"{len(mine)} just written — two writers were let in at once")
                        won += 1
                    finally:
                        try:
                            os.unlink(contended)
                        except FileNotFoundError as e:
                            raise AssertionError(f"{contended}: the file we exclusively created was gone before "
                                                 f"we could remove it") from e

                # no wins means nothing was asserted, so it does not count as completed
                if not won:
                    raise AssertionError(
                        f"{contended}: lost all {rounds} rounds over 20s, so no exclusive create was ever "
                        f"verified — the name was held throughout, which no winner should be able to do")

            elif variant == "contended_tree":
                if procs < 2:
                    result.refused[variant] = "--procs 1, so there was no other writer to contend with"
                    continue
                # every writer builds the same tree at once, then puts its own file at the bottom
                shared_tree = os.path.join(directory, "contended-tree", *(f"lvl{i}" for i in range(6)))
                os.makedirs(shared_tree, exist_ok=True)
                mine = os.path.join(shared_tree, f"w{writer}-{seq}.bin")
                body = payload(f"tree{writer}:{seq}".encode(), 4096)
                write_whole(mine, body)
                if contents(mine) != body:
                    raise AssertionError(f"{mine}: a file at the bottom of a contended tree did not read back")
                here = set(os.listdir(shared_tree))
                if os.path.basename(mine) not in here:
                    raise AssertionError(f"{mine}: written, and the directory it is in does not list it")

            # ── log rotation, both ways round, with the appender never told ──
            elif variant == "overlapping_writers":
                # several descriptors write the same range; POSIX makes each write(2) atomic with respect to
                # the others, so the range must end up holding one writer's bytes throughout. A seam means a
                # write was applied in pieces. Which writer wins is not checked.
                f = name("overlap")
                span = 256 * 1024
                marks = b"ABCD"
                fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o644)
                hs = []
                try:
                    os.ftruncate(fd, span)
                    hs = [os.open(f, os.O_WRONLY) for _ in marks]
                    for _ in range(12):
                        # One offset, every writer, in an order the store cannot predict.
                        order = list(range(len(marks)))
                        rng.shuffle(order)
                        for who in order:
                            pwrite_all(hs[who], bytes([marks[who]]) * span, 0)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                    for h in hs:
                        os.close(h)

                got = contents(f)
                if len(got) != span:
                    raise AssertionError(f"{f}: {len(got)} bytes back from a {span}-byte file")
                distinct = set(got)
                if len(distinct) != 1:
                    # the seam's offset shows the granularity the store split at
                    seam = next(i for i in range(1, span) if got[i] != got[i - 1])
                    raise AssertionError(
                        f"{f}: a range written whole by each of {len(marks)} writers came back holding "
                        f"{sorted(bytes(distinct))!r}, first changing at offset {seam}. Every one of those "
                        f"writes covered the entire range, so no ordering of them produces a mixture — the "
                        f"store applied at least one of them in pieces")
                if bytes(distinct) not in [bytes([m]) for m in marks]:
                    raise AssertionError(f"{f}: holds {bytes(distinct)!r}, which no writer ever wrote")
                os.unlink(f)

            elif variant == "mode_is_enforced":
                # recorded permission bits must be enforced, and must be possible to undo. Skipped as root,
                # which bypasses the checks.
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    result.refused[variant] = "running as root, which bypasses mode checks by design"
                    continue

                f = name("mode")
                body = payload(b"protected", 4096)
                write_whole(f, body)

                allowed(os.chmod, f, 0o400)                    # read-only, to its owner
                try:
                    fd = os.open(f, os.O_WRONLY)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                        raise
                else:
                    os.close(fd)
                    raise AssertionError(
                        f"{f}: opened for writing at mode 0400 — the mode is recorded and not enforced, so "
                        f"every tool that shows this file as protected is wrong about it")

                if contents(f) != body:
                    raise AssertionError(f"{f}: mode 0400 is read-only, and it does not read")

                allowed(os.chmod, f, 0o000)                    # nothing, to anybody
                try:
                    fd = os.open(f, os.O_RDONLY)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EPERM):
                        raise
                else:
                    os.close(fd)
                    raise AssertionError(f"{f}: opened for reading at mode 0000")

                # And back. A mode you cannot take off is a file you have lost.
                allowed(os.chmod, f, 0o644)
                if contents(f) != body:
                    raise AssertionError(
                        f"{f}: restored to 0644 and the bytes are gone — the permission round trip cost the "
                        f"file, which is the one outcome worse than not enforcing modes at all")
                os.unlink(f)

            elif variant == "log_rotation":
                # logrotate, with the appender never told:
                #   rename    renamed aside and recreated; writes after the rename land in the renamed file only
                #   copytrunc copied and truncated under the writer; with O_APPEND the next write goes to 0,
                #             so no leading hole of zeros
                d2 = os.path.join(d, f"rotate-{seq}")
                os.makedirs(d2, exist_ok=True)
                live = os.path.join(d2, "service.log")
                frame = payload(b"logline", 512)
                fh = os.open(live, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    for _ in range(8):
                        write_all(fh, frame)
                    rotated = live + ".1"
                    os.rename(live, rotated)                    # the appender is not told
                    os.close(os.open(live, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                    for _ in range(8):
                        write_all(fh, frame)                    # must follow the FILE, not the name
                    os.fsync(fh)
                    if len(contents(rotated)) != 16 * len(frame):
                        raise AssertionError(
                            f"{rotated}: {len(contents(rotated))} bytes after the rotation and 16 frames "
                            f"({16 * len(frame)}) were written to the handle — the appender followed the "
                            f"NAME across a rename instead of the file it had open")
                    if os.path.getsize(live) != 0:
                        raise AssertionError(
                            f"{live}: the freshly created log holds {os.path.getsize(live)} bytes — "
                            f"writes to a handle opened before the rotation landed in the new file")
                finally:
                    os.close(fh)

                fh = os.open(live, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    for _ in range(8):
                        write_all(fh, frame)
                    os.fsync(fh)
                    shutil.copyfile(live, live + ".2")
                    os.truncate(live, 0)                        # copytruncate, under a live appender
                    write_all(fh, frame)
                    os.fsync(fh)
                    after = contents(live)
                    if after.startswith(bytes(len(frame))):
                        raise AssertionError(
                            f"{live}: after a copytruncate the file begins with {len(frame)} zero bytes and "
                            f"is {len(after)} long — an O_APPEND write went to the offset the writer "
                            f"remembered rather than to the end of the file, so the log is a hole")
                    if after != frame:
                        raise AssertionError(
                            f"{live}: {len(after)} bytes after a copytruncate and one appended frame, "
                            f"expected exactly {len(frame)}")
                finally:
                    os.close(fh)
                shutil.rmtree(d2, ignore_errors=True)

            # ── things that must fail cleanly, or succeed exactly, but never corrupt ──
            else:
                f = name("neg")
                body = payload(b"neg", rng.choice([1, 4096, 32768]))
                with open(f, "wb") as fh:
                    fh.write(body)
                # rename onto itself: POSIX says succeed and change nothing
                allowed(os.rename, f, f)
                if contents(f) != body:
                    raise AssertionError(f"{f}: renaming a file onto its own name changed it")
                # hardlink: a protocol without one may refuse, and that is right — silently doing nothing is not
                link = f + ".link"
                try:
                    allowed(os.link, f, link)
                    if contents(link) != body:
                        raise AssertionError(f"{link}: link() reported success and the result differs")
                    os.unlink(link)
                except Refused:
                    if os.path.exists(link):
                        raise AssertionError(f"{link}: link() refused and still left a file behind")
                # write into a handle whose parent directory is gone
                sub = os.path.join(d, f"doomed-{seq}")
                os.makedirs(sub, exist_ok=True)
                orphan = os.path.join(sub, "orphan.bin")
                oh = os.open(orphan, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    write_all(oh, body)
                    os.unlink(orphan)

                    # a network client keeps an unlinked open file under a silly-rename, so the parent may not be empty
                    try:
                        os.rmdir(sub)
                    except OSError as e:
                        if e.errno != errno.ENOTEMPTY:
                            raise
                        left = os.listdir(sub)
                        if not any(n.startswith((".nfs", ".fuse_hidden", ".__smb")) for n in left):
                            raise AssertionError(
                                f"{sub}: rmdir said ENOTEMPTY after its only file was unlinked, and what is "
                                f"left is {left!r} — not a stashed open file, so the unlink did not happen")

                    try:
                        write_all(oh, body)                     # into a file whose name is gone
                    except OSError as e:
                        if e.errno not in DESTROYED_REFUSALS:
                            raise
                finally:
                    os.close(oh)
                    shutil.rmtree(sub, ignore_errors=True)
                if os.path.exists(sub):
                    for leftover in os.listdir(sub):
                        os.unlink(os.path.join(sub, leftover))
                    os.rmdir(sub)
                os.unlink(f)

            result.done[variant] += 1
        except Refused as r:
            result.refused.setdefault(variant, str(r))
        except Exception as exc:                              # noqa: BLE001 — recorded, and we keep going
            result.record(variant, exc)

    # returned rather than printed, so the verdict reports a refused variant as a gap, not as never reached
    return result


# ── the inode phase ─────────────────────────────────────────────────────────────────────────────────
# Backup and sync tools key on (dev, ino): the same ino at a new path is a move, a new ino a new file. borg's
# files cache, GNU tar --listed-incremental, git's index and rsync -H all rely on it, and the Linux NFS
# client returns ESTALE when a reply's fileid changes. Colliding or path-derived ids corrupt all of them.
INODE_TREE_DIRS = 6
INODE_TREE_FILES = 40           # per directory: enough tree for the algebra, cheap enough to repeat freely
INODE_BODY = 64


@dataclass(frozen=True)
class Seen:
    """One file as a scanner sees it. d_ino (from READDIR) and ino (from stat) come from different server
    code paths and must agree."""
    ino: int
    d_ino: int
    dev: int
    nlink: int
    digest: str


def walk_identities(root: str) -> dict[str, Seen]:
    """Walks like a backup scanner (getdents for d_ino, lstat for the rest) and checks the two agree.

    Uses scandir rather than stat per known name: after a rename the Linux client can answer a stat from
    its own dentry cache, which would hide the change.
    """
    seen: dict[str, Seen] = {}
    for parent, _, _ in os.walk(root):
        with os.scandir(parent) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = os.lstat(entry.path)
                if entry.inode() != st.st_ino:
                    raise AssertionError(
                        f"{entry.path}: the listing says inode {entry.inode()} and a stat says {st.st_ino} — "
                        f"two answers to which file this is, so `ls -i` and `find` are reading a different "
                        f"filesystem from the one `stat` is")
                if st.st_nlink != 1:
                    raise AssertionError(
                        f"{entry.path}: nlink is {st.st_nlink} for a file nothing ever linked — tar and "
                        f"rsync -H would emit a link record instead of the bytes, and du would stop "
                        f"counting it")
                with open(entry.path, "rb") as fh:
                    body = fh.read()
                seen[os.path.relpath(entry.path, root)] = Seen(
                    st.st_ino, entry.inode(), st.st_dev, st.st_nlink, hashlib.sha256(body).hexdigest())
    return seen


def hammer_inode(directory: str, writer: int, deadline: float,
                 rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """A rename moves an object; it never creates, destroys or exchanges one.

    Each variant catches a server that derives identity from the path rather than the file. st_dev over one
    mount is constant and carries no weight here.
    """
    result = PhaseResult.over(("unique_inodes", "rename_keeps_inode", "subtree_move_keeps_inodes",
                               "rename_to_self", "case_only_rename", "rename_over_kills_target",
                               "three_rename_swap", "readdir_agrees_under_mutation",
                               "directory_link_count_is_usable", "directory_size_is_usable"))
    base = os.path.join(directory, f"w{writer}-inode")
    os.makedirs(base, exist_ok=True)
    seq = 0

    def plant(root: str, dirs: int = INODE_TREE_DIRS, per: int = INODE_TREE_FILES) -> None:
        for i in range(dirs):
            d = os.path.join(root, f"d{i}")
            os.makedirs(d, exist_ok=True)
            for j in range(per):
                write_whole(os.path.join(d, f"f{j:03d}.bin"), payload(f"{i}:{j}".encode(), INODE_BODY))

    def one_of(tree: dict[str, Seen], pick: random.Random) -> str:
        return pick.choice(sorted(tree))

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "inode"):
        seq += 1
        root = os.path.join(base, f"tree-{seq}")
        try:
            if variant == "unique_inodes":
                # distinct files need distinct inode numbers; a server folding wider ids (a GUID index) into
                # 64 bits can collide
                plant(root)
                tree = walk_identities(root)
                distinct = {s.ino for s in tree.values()}
                if len(distinct) != len(tree):
                    raise AssertionError(
                        f"{root}: {len(tree)} files share only {len(distinct)} inode numbers — rsync -H, "
                        f"tar and du would each treat unrelated files as one object")

            elif variant == "rename_keeps_inode":
                plant(root, dirs=2, per=8)
                before = walk_identities(root)
                name = one_of(before, rng)
                moved = os.path.join(os.path.dirname(name), "renamed.bin")
                os.rename(os.path.join(root, name), os.path.join(root, moved))
                after = walk_identities(root)
                if moved not in after:
                    raise AssertionError(f"{root}/{moved}: the renamed file is not in the listing")
                if after[moved].ino != before[name].ino:
                    raise AssertionError(
                        f"{root}/{moved}: inode {after[moved].ino} where {before[name].ino} moved in from "
                        f"{name} — the rename made a NEW object, so every backup re-reads the tree and a "
                        f"move-detecting one loses the link")
                if after[moved].digest != before[name].digest:
                    raise AssertionError(f"{root}/{moved}: same object after the move, different bytes")

            elif variant == "subtree_move_keeps_inodes":
                # many inodes re-prefixed at once; path-derived identity loses every one
                plant(root, dirs=3, per=12)
                inner = os.path.join(root, "d0")
                before = {k: v for k, v in walk_identities(root).items() if k.startswith("d0" + os.sep)}
                landed = os.path.join(root, "d1", "carried")
                allowed(os.rename, inner, landed)
                after = walk_identities(root)
                for name, was in before.items():
                    now = after.get(os.path.join("d1", "carried", os.path.relpath(name, "d0")))
                    if now is None:
                        raise AssertionError(f"{root}: {name} did not arrive at the tree's new address")
                    if now.ino != was.ino:
                        raise AssertionError(
                            f"{root}: {name} came through the subtree move as inode {now.ino}, having been "
                            f"{was.ino} — moving a directory re-made every object beneath it")

            elif variant == "rename_to_self":
                # POSIX: renaming a file onto itself succeeds and does nothing. Unlinking the target first
                # deletes the file.
                plant(root, dirs=1, per=2)
                before = walk_identities(root)
                name = one_of(before, rng)
                full = os.path.join(root, name)
                os.rename(full, full)
                after = walk_identities(root)
                if name not in after:
                    raise AssertionError(
                        f"{full}: renaming a file to its own name DELETED it — POSIX says succeed and do "
                        f"nothing else")
                if after[name].ino != before[name].ino or after[name].digest != before[name].digest:
                    raise AssertionError(f"{full}: renaming a file to its own name replaced the object under it")

            elif variant == "case_only_rename":
                # f.bin -> F.bin: a new name on an ordinal store, a self-rename on a folding one. Either is
                # fine; the file must exist afterwards.
                os.makedirs(root, exist_ok=True)
                lower, upper = os.path.join(root, "case.bin"), os.path.join(root, "CASE.bin")
                body = payload(b"case", 512)
                write_whole(lower, body)
                folds = os.path.exists(upper)
                was = os.lstat(lower).st_ino
                os.rename(lower, upper)
                if contents(upper) != body:
                    raise AssertionError(f"{upper}: a case-only rename did not bring the bytes with it")
                if os.lstat(upper).st_ino != was:
                    raise AssertionError(f"{upper}: a case-only rename made a new object")
                if not folds and os.path.exists(lower):
                    raise AssertionError(
                        f"{lower}: still there after being renamed to {os.path.basename(upper)} on a store "
                        f"that does not fold case — one rename left two names on one object")

            elif variant == "rename_over_kills_target":
                # the replaced file's identity must stop resolving, or an old handle reads its replacement
                plant(root, dirs=1, per=4)
                before = walk_identities(root)
                victim, source = sorted(before)[0], sorted(before)[1]
                doomed = before[victim].ino
                os.rename(os.path.join(root, source), os.path.join(root, victim))
                after = walk_identities(root)
                if after[victim].ino != before[source].ino:
                    raise AssertionError(
                        f"{root}/{victim}: the source did not keep its identity across a replacing rename")
                if any(s.ino == doomed for s in after.values()):
                    raise AssertionError(
                        f"{root}: inode {doomed} was overwritten by a rename and is still reachable — the "
                        f"replaced file came back somewhere else")

            elif variant == "three_rename_swap":
                # two files swap names; with path-keyed identity neither appears to move
                os.makedirs(root, exist_ok=True)
                a, b, tmp = (os.path.join(root, n) for n in ("a.bin", "b.bin", "swap.tmp"))
                body_a, body_b = payload(b"aaa", 1024), payload(b"bbb", 1024)
                write_whole(a, body_a)
                write_whole(b, body_b)
                ino_a, ino_b = os.lstat(a).st_ino, os.lstat(b).st_ino
                os.rename(a, tmp)
                os.rename(b, a)
                os.rename(tmp, b)
                if os.lstat(a).st_ino != ino_b or os.lstat(b).st_ino != ino_a:
                    raise AssertionError(
                        f"{root}: after a three-rename swap a is inode {os.lstat(a).st_ino} and b is "
                        f"{os.lstat(b).st_ino}, where they began as {ino_a} and {ino_b} — the identities "
                        f"followed the paths instead of the files")
                if contents(a) != body_b or contents(b) != body_a:
                    raise AssertionError(f"{root}: the swap moved the names and left the bytes behind")

            elif variant == "directory_size_is_usable":
                # a directory's st_size is filesystem-specific (ext4: block-granular by name bytes; tmpfs:
                # 40 + 20 per entry), so only two properties are checked: non-zero, since tools size readdir
                # buffers from it, and stable while nothing changes, since a moving size makes clients
                # drop their caches on every stat
                d = os.path.join(base, f"dirsize-{seq}")
                os.makedirs(d, exist_ok=True)
                for n in range(12):
                    with open(os.path.join(d, f"entry-{n}.bin"), "wb") as fh:
                        fh.write(payload(b"dirsize", 64))

                first = os.stat(d).st_size
                if first <= 0:
                    raise AssertionError(f"{d}: a directory reports st_size {first}; nothing real reports "
                                         f"zero, so callers that size a buffer from it get nothing")

                # list in between stats: a server computing size from children may answer a listing differently
                os.listdir(d)
                again = os.stat(d).st_size
                os.listdir(d)
                third = os.stat(d).st_size
                if not first == again == third:
                    raise AssertionError(f"{d}: st_size moved without the directory changing — "
                                         f"{first}, {again}, {third}. A client reads that as a modification "
                                         f"and re-reads the directory on every stat")

            elif variant == "directory_link_count_is_usable":
                # find treats nlink - 2 as an upper bound on subdirectories and stops descending after that
                # many, so under-counting hides subtrees from it. Valid: 1 (btrfs; find -noleaf), 2 + subdirs
                # (ext4, XFS), 2 + every entry (APFS). Anything below the true subdirectory count is wrong.
                d = os.path.join(base, f"nlink-{seq}")
                os.makedirs(d, exist_ok=True)
                subdirs = 3
                for i in range(subdirs):
                    os.mkdir(os.path.join(d, f"sub{i}"))
                # adding a plain file must not change it
                for i in range(4):
                    write_whole(os.path.join(d, f"file{i}.bin"), payload(b"nlink", 64))

                links = os.stat(d).st_nlink
                if links != 1 and links < 2 + subdirs:
                    raise AssertionError(
                        f"{d}: holds {subdirs} subdirectories and reports nlink={links}. find reads that as "
                        f"at most {max(0, links - 2)} subdirectories and stops descending once it has seen "
                        f"that many, so {2 + subdirs - links} of them are invisible to it — and to every "
                        f"backup built on find. Report at least 2 + subdirectories, or report 1 and let "
                        f"find fall back to -noleaf")
                shutil.rmtree(d, ignore_errors=True)

            else:  # readdir_agrees_under_mutation
                # d_ino == st_ino is checked on every entry; churn exposes servers that number by listing position
                plant(root, dirs=2, per=10)
                for round_no in range(4):
                    tree = walk_identities(root)
                    name = one_of(tree, rng)
                    os.rename(os.path.join(root, name),
                              os.path.join(root, os.path.dirname(name), f"churn-{round_no}.bin"))
                    write_whole(os.path.join(root, "d0", f"new-{round_no}.bin"),
                                payload(f"n{round_no}".encode(), INODE_BODY))
                walk_identities(root)

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    return result


# ── the publish phase ───────────────────────────────────────────────────────────────────────────────
# Applications publish a set of files by switching one pointer: LevelDB's CURRENT, git refs, HDFS
# checkpoint sidecars, maildir links, deploy symlinks. The shared failure is a name visible before the bytes
# it refers to. Locally that needs a crash (ext4 delayed allocation); between two client sessions it needs
# nothing, so the ordering is the server's job. See ALICE and CrashMonkey for how common these bugs are.
#
# Not covered: a store that wrote both files in the wrong order, or would replay them wrongly after a
# crash. That needs a crash between the two writes.
PUBLISH_SIZES = [1, 512, 4096, 4097, 65536]
PUBLISH_PARTS = (2, 5)                  # data files per generation
# manifest pads so the read straddles a page boundary
PUBLISH_MANIFEST_PADS = [0, 4095, 4096, 4097]
POINTER = struct.Struct("<4sQI")        # magic, generation, crc32 of the two fields before it
POINTER_MAGIC = b"CURR"


@dataclass(frozen=True)
class Part:
    """One published file, as the manifest that names it describes it."""
    name: str
    length: int
    crc: int


@dataclass(frozen=True)
class Manifest:
    """A generation: the files it publishes, and its own number, so a pointer resolving to the wrong
    manifest is caught."""
    generation: int
    parts: list[Part]

    def encode(self, pad: int) -> bytes:
        body = f"{self.generation}\n" + "".join(f"{p.name} {p.length} {p.crc}\n" for p in self.parts)
        raw = body.encode()
        return raw + f"CRC {binascii.crc32(raw)}\n".encode() + b"\n" * pad

    @staticmethod
    def decode(raw: bytes, where: str) -> "Manifest":
        text = raw.decode(errors="replace").rstrip("\n")
        lines = text.split("\n")
        if not lines or not lines[-1].startswith("CRC "):
            raise AssertionError(f"{where}: the manifest has no CRC line — it was read while half-written, "
                                 f"or the pointer named it before it was whole")
        stated = int(lines[-1].split()[1])
        rebuilt = ("\n".join(lines[:-1]) + "\n").encode()
        if binascii.crc32(rebuilt) != stated:
            raise AssertionError(f"{where}: the manifest's own CRC does not check — {len(raw)} bytes read")
        parts = []
        for line in lines[1:-1]:
            name, length, crc = line.rsplit(" ", 2)
            parts.append(Part(name, int(length), int(crc)))
        return Manifest(int(lines[0]), parts)


def fsync_dir(path: str) -> None:
    """fsync a directory, making its entries durable (LevelDB, SQLite and git depend on this; see LevelDB
    issue #189). EINVAL is how a filesystem says it cannot sync directories; any other error is a failure."""
    fd = os.open(path, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0)))
    try:
        allowed(os.fsync, fd, refusals={errno.EINVAL})
    finally:
        os.close(fd)


def write_durably(path: str, body: bytes) -> None:
    """Bytes on the server before anything is allowed to name them."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        write_all(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)


def hammer_publish(directory: str, writer: int, deadline: float,
                   rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Publish a set of files, then switch one pointer to make them live.

    Whatever generation a reader resolves through the pointer, every file it names must exist with the
    recorded length and CRC, and no reader may resolve an older generation than one it already saw through
    the same mount. Generations are never deleted and names never reused, so a lagging pointer or a cached
    negative lookup is still consistent.

    With --server-root the published generation is also read directly from the server's directory.
    """
    shapes = ["publish_rename", "publish_link", "publish_sidecar_first", "publish_boundary_manifest"]
    # following another writer needs another writer; main() reports the gap at --procs 1
    if procs > 1:
        shapes.append("follow_other_writers")
    result = PhaseResult.over(shapes)
    root = os.path.join(directory, "publish")
    mine = os.path.join(root, f"w{writer}")
    for sub in ("data", "manifests"):
        os.makedirs(os.path.join(mine, sub), exist_ok=True)
    # per (mount, publisher): each mount may lag, neither may go backwards
    high_water: dict[str, int] = {}
    generation = 0

    def data_for(gen: int) -> list[Part]:
        parts = []
        for i in range(rng.randint(*PUBLISH_PARTS)):
            body = payload(f"{writer}:{gen}:{i}".encode(), rng.choice(PUBLISH_SIZES))
            name = f"gen-{gen:06d}-{i}.bin"
            write_durably(os.path.join(mine, "data", name), body)
            parts.append(Part(name, len(body), binascii.crc32(body)))
        fsync_dir(os.path.join(mine, "data"))   # entries durable BEFORE anything names them
        return parts

    def verify(under: str, gen: int, read) -> None:
        """Everything the pointer's generation promises, at whatever address `under` names it."""
        manifest = Manifest.decode(read(os.path.join(under, "manifests", f"manifest-{gen:06d}")),
                                   f"{under}/manifests/manifest-{gen:06d}")
        if manifest.generation != gen:
            raise AssertionError(f"{under}: the pointer named generation {gen} and the manifest it resolved "
                                 f"to says it is generation {manifest.generation} — a mis-delivered read")
        for part in manifest.parts:
            full = os.path.join(under, "data", part.name)
            size = os.stat(full).st_size
            if size != part.length:
                raise AssertionError(
                    f"{full}: the pointer named it at {part.length} bytes and it is {size} "
                    f"({size - part.length:+d}) — the NAME became visible before the BYTES")
            if binascii.crc32(read(full)) != part.crc:
                raise AssertionError(f"{full}: right length, wrong bytes — published half-written")

    def read_pointer(under: str, who: str, read) -> bytes:
        """CURRENT's bytes, retrying once on ESTALE.

        The flip renames over CURRENT, so a follower's open handle can name an unlinked file, which NFS
        answers with ESTALE (RFC 1813). The retry must show the pointer moved: if it still names the
        generation already resolved, nothing replaced it and the file was lost. A second ESTALE propagates.
        """
        try:
            return read(os.path.join(under, "CURRENT"))
        except OSError as e:
            if e.errno != errno.ESTALE:
                raise
        raw = read(os.path.join(under, "CURRENT"))
        resolved = high_water.get(who)
        if resolved is not None and len(raw) == POINTER.size:
            _, gen, _ = POINTER.unpack(raw)
            if gen == resolved:
                raise AssertionError(
                    f"{under}/CURRENT: a stale handle on a pointer that still names generation {gen}. "
                    f"Nothing replaced it, so this is a live file the store stopped being able to serve "
                    f"rather than a flip caught mid-read")
        return raw

    def follow(under: str, who: str, read) -> None:
        raw = read_pointer(under, who, read)
        if len(raw) != POINTER.size:
            raise AssertionError(f"{under}/CURRENT: {len(raw)} bytes, and a pointer record is "
                                 f"{POINTER.size} — rename(2) was not atomic")
        magic, gen, crc = POINTER.unpack(raw)
        if magic != POINTER_MAGIC or binascii.crc32(raw[:POINTER.size - 4]) != crc:
            raise AssertionError(f"{under}/CURRENT: the pointer record is not whole — a reader caught the "
                                 f"rename half-done")
        if gen < high_water.get(who, 0):
            raise AssertionError(
                f"{under}/CURRENT: names generation {gen} after this mount already resolved "
                f"{high_water[who]} — a cache may LAG, it may not REWIND")
        high_water[who] = gen
        verify(under, gen, read)

    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "publish"):
        try:
            if variant == "follow_other_writers":
                # half through the other mount, where name/bytes ordering is the server's to get right
                followed = 0
                for other in sorted(os.listdir(root)):
                    if other == f"w{writer}":
                        continue
                    theirs = os.path.join(root, other)
                    if not os.path.exists(os.path.join(theirs, "CURRENT")):
                        continue                # they have not published a first generation yet
                    through = via_alt_mount(theirs) if (followed % 2 and _ALT_ROOT) else theirs
                    follow(through, f"{through}:{other}", contents if followed % 2 else read_plainly)
                    followed += 1
                if not followed:
                    # no other writer has published yet, so this turn proves nothing
                    raise Refused("no other writer had published a first generation yet, so there was "
                                  "nothing to follow")

            else:
                generation += 1
                gen = generation
                parts = data_for(gen)
                # the next generation's data lands before this pointer flips, so unnamed files are always present
                data_for(gen + 1)

                pad = PUBLISH_MANIFEST_PADS[gen % len(PUBLISH_MANIFEST_PADS)] \
                    if variant == "publish_boundary_manifest" else 0
                manifest = Manifest(gen, parts)
                target = os.path.join(mine, "manifests", f"manifest-{gen:06d}")
                if variant == "publish_link":
                    # git/maildir style: write a temp, fsync, link into place, unlink the temp. A store with
                    # linkmax=1 may refuse; that gap is this variant's alone
                    staging = target + ".tmp"
                    write_durably(staging, manifest.encode(pad))
                    allowed(os.link, staging, target)
                    os.unlink(staging)
                else:
                    write_durably(target, manifest.encode(pad))
                fsync_dir(os.path.join(mine, "manifests"))

                if variant == "publish_sidecar_first":
                    # HDFS order: the sidecar is renamed into place before the file it describes
                    sidecar = os.path.join(mine, "manifests", f"manifest-{gen + 1:06d}.crc")
                    write_durably(sidecar, f"{binascii.crc32(manifest.encode(0))}\n".encode())
                    fsync_dir(os.path.join(mine, "manifests"))

                staged = os.path.join(mine, f"CURRENT.tmp.{os.getpid()}")
                head = POINTER_MAGIC + struct.pack("<Q", gen)
                write_durably(staged, POINTER.pack(POINTER_MAGIC, gen, binascii.crc32(head)))
                os.rename(staged, os.path.join(mine, "CURRENT"))
                fsync_dir(mine)

                if os.path.exists(staged):
                    raise AssertionError(f"{staged}: rename(2) returned and the source is still there")
                follow(mine, f"{mine}:self", contents)
                if _SERVER_ROOT and _MOUNT_ROOT:
                    # read around the client: the store must not hold a pointer to data it does not hold
                    verify(os.path.join(_SERVER_ROOT, os.path.relpath(mine, _MOUNT_ROOT)), gen, read_plainly)

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)

    return result


# ── the walk phase ──────────────────────────────────────────────────────────────────────────────────
# Every other phase opens paths it made itself; this one believes listings. READDIRPLUS returns a name, a
# handle and attributes in one reply, and a server filling them from different places binds a name to the
# wrong object (knfsd before 43b0e7ea590b; paging bugs as in irods_client_nfsrods #77).
#
# Each planted file starts with a frame naming its own path, and its length depends only on that path, so
# any entry can be validated without coordination. Planted names are renamed into place, churn uses its own
# prefix, and each writer's subtree is only touched by that writer, so nothing depends on timing.
WALK_TREE = "sweep"
WALK_DEPTH, WALK_FANOUT, WALK_FILES = 3, 3, 12
WALK_PAGED_ENTRIES = 900                # past several READDIRPLUS reply pages in one directory
WALK_CHURN_PREFIX = "chrn-"             # the ONLY namespace churn may touch
WALK_TEMP_PREFIX = ".tmp-"
WALK_MAGIC = b"SWP1"
WALK_HEAD = struct.Struct("<4sHI")      # magic, path length, crc32(path)


def walk_length(relative: str) -> int:
    """How big a planted file is, known from its NAME alone — so no manifest has to cross a process."""
    return 96 + binascii.crc32(relative.encode()) % 6000


def walk_body(relative: str) -> bytes:
    name = relative.encode()
    head = WALK_HEAD.pack(WALK_MAGIC, len(name), binascii.crc32(name)) + name
    return head + payload(name, walk_length(relative) - len(head))


def walk_claim(raw: bytes, where: str) -> str:
    """The path a planted file names in its own bytes, so a name bound to the wrong object is caught."""
    if len(raw) < WALK_HEAD.size:
        raise AssertionError(f"{where}: {len(raw)} bytes, too short to carry the frame that names it")
    magic, length, crc = WALK_HEAD.unpack_from(raw)
    name = raw[WALK_HEAD.size:WALK_HEAD.size + length]
    if magic != WALK_MAGIC or binascii.crc32(name) != crc:
        raise AssertionError(f"{where}: does not begin with a planted frame — {raw[:16]!r}")
    return name.decode()


@dataclass
class Walked:
    """One sweep of a tree, as the LISTING described it."""
    paths: list[str] = field(default_factory=list)
    identities: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)


def hammer_walk(directory: str, writer: int, deadline: float,
                rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """Directory listings, checked against a tree other writers are mutating.

    Type and size comparisons prove less on a client that answers them with its own stat instead of the
    listing's d_type and READDIRPLUS attributes. d_ino and the path each file contains do not depend on that.
    """
    result = PhaseResult.over(("walk_manifest", "walk_typed", "walk_content", "walk_under_churn",
                               "walk_paged", "walk_symlink_trap", "walk_twice"))
    root = os.path.join(directory, WALK_TREE)
    mine = os.path.join(root, f"w{writer}")
    os.makedirs(mine, exist_ok=True)

    def mirror(relative: str) -> "str | None":
        """The same relative path where the SERVER keeps it, when that is readable."""
        if not _SERVER_ROOT or not _MOUNT_ROOT:
            return None
        return os.path.join(_SERVER_ROOT, os.path.relpath(os.path.join(root, relative), _MOUNT_ROOT))

    def plant(relative: str) -> None:
        """Written aside and renamed into place, so a planted name is absent or complete, never partial."""
        full = os.path.join(root, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        staged = os.path.join(os.path.dirname(full), f"{WALK_TEMP_PREFIX}w{writer}-{os.path.basename(full)}")
        write_durably(staged, walk_body(relative))
        os.rename(staged, full)

    def plant_tree() -> list[str]:
        """This writer's own tree, untouched by others, so its manifest is exact. The wide directory is
        planted here too, so the manifest covers the whole subtree."""
        planted, frontier = [], [f"w{writer}"]
        for _ in range(WALK_DEPTH):
            nxt = []
            for parent in frontier:
                for b in range(WALK_FANOUT):
                    branch = os.path.join(parent, f"b{b}")
                    os.makedirs(os.path.join(root, branch), exist_ok=True)
                    for f in range(WALK_FILES):
                        relative = os.path.join(branch, f"p{f:03d}.bin")
                        plant(relative)
                        planted.append(relative)
                    nxt.append(branch)
            frontier = nxt
        os.makedirs(os.path.join(root, f"w{writer}", "wide"), exist_ok=True)
        for i in range(WALK_PAGED_ENTRIES):
            relative = os.path.join(f"w{writer}", "wide", f"e{i:05d}.bin")
            plant(relative)
            planted.append(relative)
        return planted

    def ignorable(relative: str) -> bool:
        return os.path.basename(relative).startswith((WALK_CHURN_PREFIX, WALK_TEMP_PREFIX))

    def sweep(top: str, read=None) -> Walked:
        """One recursive walk, every oracle applied per entry, believing the listing at every step."""
        walked, stack = Walked(), [top]
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    relative = os.path.relpath(entry.path, root)
                    if ignorable(relative):
                        continue                        # churn: never asserted on, by construction
                    listed_link = entry.is_symlink()
                    listed_dir = entry.is_dir(follow_symlinks=False)
                    try:
                        listed_size = entry.stat(follow_symlinks=False).st_size
                        truth = os.lstat(entry.path)
                    except FileNotFoundError:
                        if relative.startswith(f"w{writer}{os.sep}"):
                            raise AssertionError(
                                f"{relative}: my own planted name was in the listing and is not there — "
                                f"nobody else may touch it, so the listing named something that is gone")
                        continue                        # another writer mid-plant: legitimate
                    # the type the listing gave, against the type the file has
                    truly_link, truly_dir = stat.S_ISLNK(truth.st_mode), stat.S_ISDIR(truth.st_mode)
                    if listed_link != truly_link or (not listed_link and listed_dir != truly_dir):
                        raise AssertionError(
                            f"{relative}: the listing says "
                            f"{'symlink' if listed_link else 'directory' if listed_dir else 'file'} and a "
                            f"stat says {'symlink' if truly_link else 'directory' if truly_dir else 'file'} "
                            f"— a backup reading that listing skips a subtree or descends into a file")
                    kind = "symlink" if truly_link else "directory" if truly_dir else "file"
                    walked.kinds[relative] = kind
                    walked.paths.append(relative)
                    walked.identities[relative] = entry.inode()
                    if truly_link:
                        continue                        # classified, and deliberately NOT descended
                    if truly_dir:
                        stack.append(entry.path)
                        continue
                    # the size the listing gave, against the size the name predicts
                    want = walk_length(relative)
                    walked.sizes[relative] = listed_size
                    if listed_size != want or truth.st_size != want:
                        served = mirror(relative)
                        held = os.path.getsize(served) if served and os.path.exists(served) else "unknown"
                        raise AssertionError(
                            f"{relative}: the listing reported {listed_size} bytes, a stat reports "
                            f"{truth.st_size} and the name predicts {want}; the server holds {held} — "
                            f"a scanner that believed the listing would skip this file as unchanged")
                    if read is not None:
                        # the bytes say which file the listing bound to this name
                        claimed = walk_claim(read(entry.path), entry.path)
                        if claimed != relative:
                            raise AssertionError(
                                f"the walk reached {relative} and its bytes say they belong to {claimed} — "
                                f"the listing bound this name to another object (fileid "
                                f"{entry.inode()})")
        return walked

    def churn(where: str) -> None:
        """Only ever under the churn prefix, so nothing an oracle looks at can be disturbed by it."""
        os.makedirs(where, exist_ok=True)
        # a name that sorts between two planted ones, which is what moves an index cookie under a reader
        between = os.path.join(where, f"{WALK_CHURN_PREFIX}p{rng.randrange(WALK_FILES):03d}a.bin")
        write_whole(between, payload(b"churn", 512))
        moved = os.path.join(where, f"{WALK_CHURN_PREFIX}{rng.randrange(1 << 20)}.bin")
        os.rename(between, moved)                       # rename within one directory: the Dovecot shape
        flipped = os.path.join(where, f"{WALK_CHURN_PREFIX}flip")
        shutil.rmtree(flipped, ignore_errors=True)
        if os.path.exists(flipped):
            os.unlink(flipped)
        if rng.random() < 0.5:
            os.makedirs(flipped, exist_ok=True)         # the same name, a different type, round by round
        else:
            write_whole(flipped, payload(b"flip", 64))
        os.unlink(moved)

    planted: list[str] = []
    for variant in mandatory_then_repeat(result.done, seed, writer, procs, deadline, "walk"):
        try:
            if not planted:
                planted = plant_tree()

            if variant == "walk_manifest":
                # baseline: every planted name reached once, no two sharing an identity
                walked = sweep(mine)
                reached = [p for p in walked.paths if walked.kinds[p] == "file"]
                if sorted(reached) != sorted(planted):
                    missing = sorted(set(planted) - set(reached))
                    extra = sorted(set(reached) - set(planted))
                    raise AssertionError(
                        f"{mine}: the walk reached {len(reached)} of {len(planted)} planted files — "
                        f"{len(missing)} never appeared ({missing[:3]}) and {len(extra)} appeared that were "
                        f"never planted ({extra[:3]})")
                if len(reached) != len(set(reached)):
                    raise AssertionError(f"{mine}: the walk returned the same path more than once — a page "
                                         f"boundary was crossed twice")
                ids = [walked.identities[p] for p in reached]
                if len(set(ids)) != len(ids):
                    raise AssertionError(f"{mine}: {len(ids)} files share {len(set(ids))} identities — two "
                                         f"names alias to one file for anything that keys on the fileid")

            elif variant == "walk_typed":
                # the whole shared tree: other writers' names may be absent, but what is present must be right
                sweep(root)

            elif variant == "walk_content":
                # through contents(), so the bytes come from the server rather than the page cache
                sweep(mine, read=contents)

            elif variant == "walk_under_churn":
                # other writers churn names that sort between mine during the sweep; the cookie must survive it
                here = os.path.join(mine, "b0")
                for _ in range(4):
                    churn(here)
                    sweep(mine)

            elif variant == "walk_paged":
                # one directory spanning several reply pages; an unstable cookie repeats or skips names
                wide = os.path.join(mine, "wide")
                names = [p for p in planted if os.path.dirname(p).endswith("wide")]
                seen: dict[str, int] = {}
                position = 0
                with os.scandir(wide) as entries:
                    for entry in entries:
                        relative = os.path.relpath(entry.path, root)
                        if ignorable(relative):
                            continue
                        if relative in seen:
                            raise AssertionError(
                                f"{relative}: returned twice in one listing, at {seen[relative]} and "
                                f"{position} — the client restarted a page and the server let it")
                        seen[relative] = position
                        position += 1
                if len(seen) != len(names):
                    raise AssertionError(
                        f"{wide}: one pass over {len(names)} entries returned {len(seen)} — "
                        f"{len(set(names) - set(seen))} names never came back from a directory nothing "
                        f"was removing from")

            elif variant == "walk_symlink_trap":
                # a symlink to a directory inside the tree: a walker that trusts a wrong type never terminates
                trap = os.path.join(mine, f"{WALK_CHURN_PREFIX}loop")
                if os.path.islink(trap):
                    os.unlink(trap)             # a repeat: the previous round's link, not a directory
                allowed(os.symlink, "b0", trap)
                try:
                    if not os.path.islink(trap):
                        raise AssertionError(f"{trap}: a symlink to a directory did not come back as a link")
                    with os.scandir(mine) as entries:
                        for entry in entries:
                            if entry.path != trap:
                                continue
                            if not entry.is_symlink():
                                raise AssertionError(
                                    f"{trap}: the LISTING calls it "
                                    f"{'a directory' if entry.is_dir(follow_symlinks=False) else 'a file'} "
                                    f"and it is a symlink to a directory — a backup that trusts this walks "
                                    f"the subtree again under every name that points at it")
                    sweep(mine)                     # and it must still terminate
                finally:
                    os.unlink(trap)

            else:  # walk_twice
                # the same unmutated subtree twice; a listing cache expiring in between must not change it
                first, second = sweep(mine), sweep(mine)
                if sorted(first.paths) != sorted(second.paths):
                    raise AssertionError(
                        f"{mine}: two consecutive walks of an unchanged tree disagree — "
                        f"{len(set(first.paths) ^ set(second.paths))} paths differ")
                for relative in first.paths:
                    if first.kinds[relative] != second.kinds[relative]:
                        raise AssertionError(f"{relative}: listed as {first.kinds[relative]} and then as "
                                             f"{second.kinds[relative]} with nothing having touched it")
                    if first.identities[relative] != second.identities[relative]:
                        raise AssertionError(
                            f"{relative}: fileid {first.identities[relative]} and then "
                            f"{second.identities[relative]} — the identity of an untouched file changed "
                            f"between two listings, which the NFS client answers with ESTALE")
                    if first.sizes.get(relative) != second.sizes.get(relative):
                        raise AssertionError(f"{relative}: listed at {first.sizes.get(relative)} bytes and "
                                             f"then at {second.sizes.get(relative)}")

            result.done[variant] += 1
        except Refused as refused:
            result.refused.setdefault(variant, str(refused))
        except Exception as exc:                              # noqa: BLE001 — recorded, not swallowed
            result.record(variant, exc)

    return result


# ── locks ───────────────────────────────────────────────────────────────────────────────────────
#
# struct flock per platform, written out byte by byte ('=' plus explicit 'x' padding), from offsetof on
# each platform's header:
#
#   Linux   sizeof 32: l_type 0, l_whence 2, l_start 8, l_len 16, l_pid 24
#   Darwin  sizeof 24: l_start 0, l_len 8, l_pid 16, l_type 20, l_whence 22
#
# Other platforms report a gap. getlk_names_the_holder checks the decoded pid, which validates the layout.
FLOCK_LAYOUTS = {
    "darwin": (struct.Struct("=qqihh"), ("start", "length", "pid", "kind", "whence")),
    "linux": (struct.Struct("=hhxxxxqqixxxx"), ("kind", "whence", "start", "length", "pid")),
}
FLOCK = FLOCK_LAYOUTS.get("darwin" if sys.platform == "darwin" else
                          "linux" if sys.platform.startswith("linux") else "")

LOCK_DIR = "locks"
LOCK_COUNTER = "counter.bin"                 # the arithmetic oracle: one number, incremented under a lock
LOCK_ARENA = "arena.bin"                     # where every other variant takes its ranges
# per-writer, disjoint ranges, so writers never fail each other's negative checks
LOCK_STRIDE = 4096
LOCK_ROUNDS = 64                             # locked increments per turn at the counter
# The counter's critical section: an 8-byte total, then the 16-byte witness saying who is inside it.
LOCK_WITNESS = struct.Struct("<QQ")          # writer, iteration
LOCK_WITNESS_OFF = 8
LOCK_SECTION = 8 + LOCK_WITNESS.size
LOCK_STEP = 64                               # the unit every range in the arena is built from
# seconds before an unresponsive peer is a failure rather than a hang
LOCK_PEER_PATIENCE = 60
# seconds a writer waits at the counter for the others
LOCK_RENDEZVOUS_PATIENCE = 30

LOCK_VARIANTS = ("exclusive_rmw", "conflict_excludes", "shared_is_shared", "ranges_are_ranges",
                 "getlk_names_the_holder", "close_drops_every_lock", "unlock_splits_a_range",
                 "cross_mount_excludes")

# what this process added to the shared counter; a global because every phase shares one signature
_LOCK_INCREMENTS = 0

# files the client creates on its own, excluded from exact listing comparisons and counted:
#   ._name     AppleDouble, written by macOS for extended attributes the filesystem cannot hold
#   .nfsXXXX   NFS silly-rename of a file unlinked while still open
_SIDECARS_IGNORED = 0

_HEX = "0123456789abcdefABCDEF"


def is_client_sidecar(name: str) -> bool:
    """True for a name the client made for its own bookkeeping, and false for anything a caller asked for."""
    if name.startswith("._"):
        return True
    if not name.startswith(".nfs"):
        return False
    tag = name[4:]
    # length-bounded so a user's `.nfsrc` is not excused
    return len(tag) >= 8 and all(c in _HEX for c in tag)


def not_sidecars(names) -> set:
    """The names the SERVER is answerable for, with the client's own bookkeeping counted out."""
    global _SIDECARS_IGNORED
    kept = {n for n in names if not is_client_sidecar(n)}
    _SIDECARS_IGNORED += len(set(names)) - len(kept)
    return kept


# ── extended attributes ───────────────────────────────────────────────────────────────────────────
#
# CPython only exposes os.setxattr on Linux, so on macOS the four calls go through libc. Darwin's take two
# extra arguments: position (resource forks only) and options (0 follows symlinks).
_DARWIN_XATTR = None
if sys.platform == "darwin":
    try:
        import ctypes, ctypes.util
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_uint32, ctypes.c_int]
        _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_uint32, ctypes.c_int]
        _libc.getxattr.restype = ctypes.c_ssize_t
        _libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
        _libc.listxattr.restype = ctypes.c_ssize_t
        _libc.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        _DARWIN_XATTR = _libc
    except Exception:                       # noqa: BLE001 — no libc means no xattrs, which is a refusal
        _DARWIN_XATTR = None

XATTR_SUPPORTED = hasattr(os, "setxattr") or _DARWIN_XATTR is not None


def _darwin_check(rc: int, what: str, path: str) -> None:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path) from None
    _ = what


def xattr_set(path: str, name: str, value: bytes) -> None:
    if _DARWIN_XATTR is None:
        os.setxattr(path, name, value)
        return
    _darwin_check(_DARWIN_XATTR.setxattr(path.encode(), name.encode(), value, len(value), 0, 0), "set", path)


def xattr_get(path: str, name: str) -> bytes:
    if _DARWIN_XATTR is None:
        return os.getxattr(path, name)
    size = _DARWIN_XATTR.getxattr(path.encode(), name.encode(), None, 0, 0, 0)
    _darwin_check(size, "size", path)
    buf = ctypes.create_string_buffer(size)
    got = _DARWIN_XATTR.getxattr(path.encode(), name.encode(), buf, size, 0, 0)
    _darwin_check(got, "get", path)
    return buf.raw[:got]


def xattr_list(path: str) -> list:
    if _DARWIN_XATTR is None:
        return os.listxattr(path)
    size = _DARWIN_XATTR.listxattr(path.encode(), None, 0, 0)
    _darwin_check(size, "size", path)
    if size == 0:
        return []
    buf = ctypes.create_string_buffer(size)
    got = _DARWIN_XATTR.listxattr(path.encode(), buf, size, 0)
    _darwin_check(got, "list", path)
    return [n.decode() for n in buf.raw[:got].split(b"\0") if n]


def xattr_remove(path: str, name: str) -> None:
    if _DARWIN_XATTR is None:
        os.removexattr(path, name)
        return
    _darwin_check(_DARWIN_XATTR.removexattr(path.encode(), name.encode(), 0), "remove", path)


def flock_bytes(kind: int, start: int, length: int) -> bytes:
    """A `struct flock` for this platform, built from NAMED fields rather than a positional guess."""
    packer, order = FLOCK
    field = {"kind": kind, "whence": os.SEEK_SET, "start": start, "length": length, "pid": 0}
    return packer.pack(*(field[name] for name in order))


def flock_fields(raw: bytes) -> dict[str, int]:
    packer, order = FLOCK
    return dict(zip(order, packer.unpack(raw[:packer.size])))


def take_lock(fd: int, kind: int, start: int, length: int) -> bool:
    """Non-blocking. True if the range was taken, False if a conflicting lock is held (EACCES or EAGAIN,
    as POSIX allows either). Any other error is raised."""
    try:
        fcntl.lockf(fd, kind | fcntl.LOCK_NB, length, start, os.SEEK_SET)
        return True
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def drop_lock(fd: int, start: int, length: int) -> None:
    fcntl.lockf(fd, fcntl.LOCK_UN, length, start, os.SEEK_SET)


def probe_lock(fd: int, start: int, length: int) -> dict[str, int]:
    """F_GETLK: what WOULD conflict with an exclusive lock over this range. F_UNLCK means nothing does."""
    return flock_fields(fcntl.fcntl(fd, fcntl.F_GETLK, flock_bytes(fcntl.F_WRLCK, start, length)))


def lock_peer(path: str, conn) -> None:
    """A second process holding locks on the same file, driven one command at a time.

    POSIX locks belong to the process, so a process never conflicts with itself; every negative check needs
    a peer. Each command waits for the peer's reply, so nothing depends on timing.
    """
    fd = os.open(path, os.O_RDWR)
    try:
        while True:
            cmd, kind, start, length = conn.recv()
            if cmd == "exit":
                return
            if cmd == "take":
                conn.send(take_lock(fd, kind, start, length))
            elif cmd == "drop":
                drop_lock(fd, start, length)
                conn.send(True)
            elif cmd == "probe":
                conn.send(probe_lock(fd, start, length))
            elif cmd == "reopen":
                # closing any descriptor for the file drops every lock this process holds on it
                os.close(os.open(path, os.O_RDONLY))
                conn.send(True)
            else:
                raise AssertionError(f"the peer was sent a command it does not have: {cmd}")
    finally:
        os.close(fd)


class LockPeer:
    """Starts the peer and makes sure it exits, terminating it if needed, so no lock outlives the variant."""

    def __init__(self, path: str) -> None:
        self.path, self.conn, self.proc = path, None, None

    def __enter__(self) -> "LockPeer":
        mine, theirs = Pipe()
        self.conn = mine
        self.proc = Process(target=lock_peer, args=(self.path, theirs))
        self.proc.start()
        theirs.close()          # the parent's copy, so a peer that dies makes recv() raise instead of hang
        return self

    def __exit__(self, *_) -> None:
        try:
            self.conn.send(("exit", 0, 0, 0))
        except (BrokenPipeError, OSError):
            pass                # already gone, which is the state we were asking for
        self.proc.join(LOCK_PEER_PATIENCE)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(LOCK_PEER_PATIENCE)
        self.conn.close()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def do(self, cmd: str, kind: int = 0, start: int = 0, length: int = 0):
        self.conn.send((cmd, kind, start, length))
        if not self.conn.poll(LOCK_PEER_PATIENCE):
            raise AssertionError(f"the lock peer did not answer {cmd} within {LOCK_PEER_PATIENCE}s")
        return self.conn.recv()

    def holds(self, kind: int, start: int, length: int) -> None:
        """Take a range that MUST be free, so a variant never builds on a lock it did not get."""
        if not self.do("take", kind, start, length):
            raise AssertionError(f"the peer could not take an uncontended lock on [{start}, {start + length}) "
                                 f"— its own per-writer range, which nothing else may touch")


def lock_rendezvous(root: str, writer: int, procs: int) -> int:
    """Waits, up to a bound, until every writer has reached the counter, so the counter sees contention.

    Arrival is a file per writer. Returns how many arrived.
    """
    os.close(os.open(os.path.join(root, f"arrived-{writer}"), os.O_WRONLY | os.O_CREAT, 0o644))
    give_up = time.monotonic() + LOCK_RENDEZVOUS_PATIENCE
    arrived = 0
    while time.monotonic() < give_up:
        arrived = sum(1 for name in os.listdir(root) if name.startswith("arrived-"))
        if arrived >= procs:
            return arrived
        time.sleep(0.005)
    return arrived


def lock_increment(path: str, writer: int, rounds: int) -> None:
    """Read-modify-write one counter under an exclusive lock, `rounds` times.

    The parent checks the counter equals the sum of every writer's increments. Since an 8-byte update is
    over in microseconds, overlap is also detected directly: each iteration stamps (writer, iteration)
    inside the lock and reads it back before releasing.

    Always through --dir: through two mounts, client caching could lose an update that locking did not.
    The tally is updated inside the loop, because a failed iteration has still changed the file.
    """
    global _LOCK_INCREMENTS
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        for step in range(rounds):
            # blocking: the kernel queues the waiters, so no retry loop or sleep is needed
            fcntl.lockf(fd, fcntl.LOCK_EX, LOCK_SECTION, 0, os.SEEK_SET)
            try:
                mine = LOCK_WITNESS.pack(writer, step)
                if os.pwrite(fd, mine, LOCK_WITNESS_OFF) != LOCK_WITNESS.size:
                    raise AssertionError("short write of the lock witness")
                raw = os.pread(fd, 8, 0)
                value = struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0
                if os.pwrite(fd, struct.pack("<Q", value + 1), 0) != 8:
                    raise AssertionError("short write of the 8-byte lock counter")
                _LOCK_INCREMENTS += 1
                if os.pread(fd, LOCK_WITNESS.size, LOCK_WITNESS_OFF) != mine:
                    raise AssertionError("another process was inside the critical section: the witness this "
                                         "iteration wrote under the lock had been overwritten before it "
                                         "released, so two writers held the same range at once")
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, LOCK_SECTION, 0, os.SEEK_SET)
    finally:
        os.close(fd)


def hammer_locks(directory: str, writer: int, deadline: float, rng: random.Random, seed: int, procs: int) -> PhaseResult:
    """fcntl advisory locking, the one phase whose checks require the filesystem to refuse.

    The negative variants fail when a lock is granted that should not have been, which is how a store
    that stubs locking out shows up. Each needs a second process. Lock wait times are not measured.
    """
    global _LOCK_INCREMENTS
    result = PhaseResult.over(LOCK_VARIANTS)
    if _NO_LOCK:
        for variant in LOCK_VARIANTS:
            result.refused[variant] = "--no-lock"
        return result
    if fcntl is None or not FLOCK:
        for variant in LOCK_VARIANTS:
            result.refused[variant] = f"no fcntl advisory locks on {sys.platform}"
        return result

    root = os.path.join(directory, LOCK_DIR)
    os.makedirs(root, exist_ok=True)
    counter, arena = os.path.join(root, LOCK_COUNTER), os.path.join(root, LOCK_ARENA)
    # real bytes under the lock ranges; ftruncate to a fixed size is safe for every writer to repeat
    fd = os.open(arena, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        os.ftruncate(fd, LOCK_STRIDE * (procs + 1))
    finally:
        os.close(fd)
    base = LOCK_STRIDE * writer

    for variant in mandatory_then_repeat(LOCK_VARIANTS, seed, writer, procs, deadline, "locks"):
        try:
            if variant == "exclusive_rmw":
                # first turn only; on repeats the writers are already in the phase together
                if not result.done["exclusive_rmw"]:
                    lock_rendezvous(root, writer, procs)
                lock_increment(counter, writer, LOCK_ROUNDS)

            elif variant == "conflict_excludes":
                # a filesystem that grants every request would pass every positive check here
                with LockPeer(arena) as peer:
                    peer.holds(fcntl.LOCK_EX, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)
                    try:
                        if take_lock(fd, fcntl.LOCK_EX, base + 16, 16):
                            raise AssertionError("a lock was granted INSIDE a range another process holds "
                                                 "exclusively — this filesystem does not arbitrate")
                        peer.do("drop", 0, base, LOCK_STEP)
                        if not take_lock(fd, fcntl.LOCK_EX, base + 16, 16):
                            raise AssertionError("the range stayed locked after its holder released it")
                        drop_lock(fd, base + 16, 16)
                    finally:
                        os.close(fd)

            elif variant == "shared_is_shared":
                # read locks that exclude each other serialise every reader
                with LockPeer(arena) as peer:
                    peer.holds(fcntl.LOCK_SH, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)
                    try:
                        if not take_lock(fd, fcntl.LOCK_SH, base, LOCK_STEP):
                            raise AssertionError("a shared lock excluded another SHARED lock, so every lock "
                                                 "here is exclusive whatever it was asked for")
                        drop_lock(fd, base, LOCK_STEP)
                        if take_lock(fd, fcntl.LOCK_EX, base, LOCK_STEP):
                            raise AssertionError("an exclusive lock was granted over a range held SHARED")
                    finally:
                        os.close(fd)

            elif variant == "ranges_are_ranges":
                # adjacent, never overlapping: catches a store that locks the whole file for a byte range.
                # A store that grants everything passes this one; the negative variants catch that.
                with LockPeer(arena) as peer:
                    peer.holds(fcntl.LOCK_EX, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)
                    try:
                        if not take_lock(fd, fcntl.LOCK_EX, base + LOCK_STEP, LOCK_STEP):
                            raise AssertionError("a lock on the range ADJACENT to a held one was refused, so "
                                                 "this filesystem locks whole files and calls them ranges")
                        drop_lock(fd, base + LOCK_STEP, LOCK_STEP)
                    finally:
                        os.close(fd)

            elif variant == "getlk_names_the_holder":
                # F_GETLK must name the holder; the right pid also confirms struct flock is decoded correctly
                with LockPeer(arena) as peer:
                    peer.holds(fcntl.LOCK_EX, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)
                    try:
                        held = probe_lock(fd, base + 16, 16)
                        if held["kind"] != fcntl.F_WRLCK:
                            raise AssertionError(f"F_GETLK found no exclusive lock over a range one is held "
                                                 f"on: {held}")
                        if held["pid"] != peer.pid:
                            raise AssertionError(f"F_GETLK named pid {held['pid']}; {peer.pid} holds the lock")
                        free = probe_lock(fd, base + LOCK_STEP * 8, 16)
                        if free["kind"] != fcntl.F_UNLCK:
                            raise AssertionError(f"F_GETLK reported a lock over a range nobody holds: {free}")
                    finally:
                        os.close(fd)

            elif variant == "close_drops_every_lock":
                # POSIX locks belong to the process: closing any descriptor for the file drops them all
                with LockPeer(arena) as peer:
                    peer.holds(fcntl.LOCK_EX, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)
                    try:
                        if take_lock(fd, fcntl.LOCK_EX, base, LOCK_STEP):
                            raise AssertionError("the peer's lock was not visible before the close, so this "
                                                 "variant proves nothing about what the close did")
                        peer.do("reopen")
                        if not take_lock(fd, fcntl.LOCK_EX, base, LOCK_STEP):
                            raise AssertionError("a lock survived its holder opening and closing another "
                                                 "descriptor for the same file, which POSIX says drops it")
                        drop_lock(fd, base, LOCK_STEP)
                    finally:
                        os.close(fd)

            elif variant == "unlock_splits_a_range":
                # unlocking the middle of a range leaves two held ranges, not none
                fd = os.open(arena, os.O_RDWR)
                try:
                    if not take_lock(fd, fcntl.LOCK_EX, base, LOCK_STEP * 3):
                        raise AssertionError("could not take an uncontended lock on this writer's own range")
                    drop_lock(fd, base + LOCK_STEP, LOCK_STEP)
                    with LockPeer(arena) as peer:
                        if peer.do("take", fcntl.LOCK_EX, base, LOCK_STEP):
                            raise AssertionError("the FIRST third was released by an unlock of the middle")
                        if not peer.do("take", fcntl.LOCK_EX, base + LOCK_STEP, LOCK_STEP):
                            raise AssertionError("the middle third stayed locked after being unlocked")
                        peer.do("drop", 0, base + LOCK_STEP, LOCK_STEP)
                        if peer.do("take", fcntl.LOCK_EX, base + LOCK_STEP * 2, LOCK_STEP):
                            raise AssertionError("the LAST third was released by an unlock of the middle")
                    drop_lock(fd, base, LOCK_STEP)
                    drop_lock(fd, base + LOCK_STEP * 2, LOCK_STEP)
                finally:
                    os.close(fd)

            else:  # cross_mount_excludes
                # a lock taken through one mount must exclude through the other. Either the server's lock
                # manager or a client sharing one superblock between the mounts (Linux `sharecache`, the
                # default) can provide that; `nosharecache` leaves it to the server.
                if not _ALT_ROOT:
                    result.refused[variant] = ("no --alt-dir, so both processes would share one mount and "
                                               "the client's own kernel would answer instead of the store")
                    continue

                theirs = via_alt_mount(arena)
                if theirs is None or not os.path.exists(theirs):
                    result.refused[variant] = "the arena is not reachable through --alt-dir"
                    continue

                base = LOCK_SECTION + writer * LOCK_STRIDE + LOCK_STEP * 7
                with LockPeer(theirs) as peer:              # the SECOND mount, in another process
                    peer.holds(fcntl.LOCK_EX, base, LOCK_STEP)
                    fd = os.open(arena, os.O_RDWR)          # ours, through the FIRST mount
                    try:
                        excluded = not take_lock(fd, fcntl.LOCK_EX, base, LOCK_STEP)
                    finally:
                        if not excluded:
                            drop_lock(fd, base, LOCK_STEP)
                        os.close(fd)

                if not excluded:
                    raise AssertionError(
                        f"a lock held on {theirs} through the second mount was granted AGAIN on {arena} "
                        f"through the first. One export mounted twice is one set of files, so two writers "
                        f"now both believe they hold it — which is how a database or a lockfile reached by "
                        f"two paths gets destroyed. Neither the server nor the client is arbitrating; on "
                        f"Linux, check whether these mounts were given `nosharecache`")

            result.done[variant] += 1
        except Refused as r:
            result.refused.setdefault(variant, str(r))
        except Exception as exc:                              # noqa: BLE001 — filed under its variant
            result.record(variant, exc)
    return result


@dataclass(frozen=True)
class VariantPhase:
    """A phase that rotates a set of variants and reports completed, failed and refused for each."""
    key: str                                            # what it travels under, and what the verdict calls it
    share: float                                        # its slice of the clock, spent only after the mandatory pass
    run: "object"                                       # (directory, writer, deadline, rng, seed, procs) -> PhaseResult
    title: str = ""                                     # when the verdict wants a longer name than the key

    def named(self) -> str:
        return self.title or self.key


VARIANT_PHASES = (
    VariantPhase("malice", MALICE_PHASE_SHARE, hammer_malice),
    VariantPhase("identity", IDENTITY_PHASE_SHARE, hammer_identity),
    VariantPhase("namespace", NAMESPACE_PHASE_SHARE, hammer_namespace),
    VariantPhase("sqlite", SQLITE_PHASE_SHARE, hammer_sqlite, title="sqlite-shaped"),
    VariantPhase("publish", PUBLISH_PHASE_SHARE, hammer_publish),
    VariantPhase("inode", INODE_PHASE_SHARE, hammer_inode),
    VariantPhase("walk", WALK_PHASE_SHARE, hammer_walk),
    VariantPhase("locks", LOCKS_PHASE_SHARE, hammer_locks),
    VariantPhase("contest", CONTEST_PHASE_SHARE, hammer_contest),
)


@dataclass
class WriterReport:
    """Everything one writer sends the parent."""
    writer: int
    digests: dict[str, Digest] = field(default_factory=dict)
    downloads: dict[str, Downloaded] = field(default_factory=dict)
    download_flavours: dict[str, int] = field(default_factory=dict)
    shared: list[AckedBlock] = field(default_factory=list)
    observed: list[AckedBlock] = field(default_factory=list)
    observer: dict[str, int] = field(default_factory=dict)
    open_refusals: dict[int, int] = field(default_factory=dict)
    phases: dict[str, PhaseResult] = field(default_factory=dict)
    lock_increments: int = 0
    sidecars_ignored: int = 0
    error: "str | None" = None


def worker(directory: str, shared_dir: str, writer: int, deadline: float, seed: int, procs: int, out: Queue) -> None:
    rng = random.Random(seed * 1000 + writer)
    report = WriterReport(writer)
    models: dict[str, bytearray] = {}
    try:
        # each phase gets a share of the whole run, not whatever is left after the others
        start = time.monotonic()
        budget = deadline - start
        at = start + budget * OWN_PHASE_SHARE
        try:
            hammer_own(directory, writer, at, rng, models)
        finally:
            # in a finally, so a writer that failed still hands over its models for verification
            report.digests = {path: Digest(hashlib.sha256(m).hexdigest(), len(m)) for path, m in models.items()}
        # check bytes, not the dict: hammer_own seeds an empty model per file, which would hash-match trivially
        if not any(models.values()):
            raise AssertionError("the own-files phase wrote nothing — its share of the run was already spent")
        models.clear()                      # hundreds of MB of models, and the next phase wants the room

        at += budget * DOWNLOAD_PHASE_SHARE
        downloaded = hammer_download(directory, writer, at, rng)
        report.downloads, report.download_flavours = downloaded.finished, downloaded.flavours
        for phase in VARIANT_PHASES:
            at += budget * phase.share
            # seeded first, so a phase that raises still reports what it completed
            report.phases[phase.key] = PhaseResult.over(())
            # a declared phase is not called at all: its setup could fail on a filesystem excused from it
            if declared_gap(phase.key):
                continue
            report.phases[phase.key] = phase.run(directory, writer, at, rng, seed, procs)
            # record declared variants as refused, or the verdict reads them as never reached
            for declared in report.phases[phase.key].done:
                if declared_gap(phase.key, declared):
                    report.phases[phase.key].refused.setdefault(declared, "--declare-gap")
        # Read after the phases rather than returned from one: see the note at _LOCK_INCREMENTS.
        report.lock_increments = _LOCK_INCREMENTS
        report.sidecars_ignored = _SIDECARS_IGNORED
        at += budget * OBSERVER_PHASE_SHARE
        report.observer = hammer_observer(shared_dir, writer, at, rng, report.observed)
        # not run when declared: the writers would only destroy each other's blocks
        if not declared_gap("shared"):
            hammer_shared(os.path.join(shared_dir, SHARED), writer, deadline, rng, report.shared, report.open_refusals)
    except Exception as exc:                                  # noqa: BLE001 — reported, not swallowed
        # include where it was raised; an errno alone names neither the operation nor the file
        frames = traceback.extract_tb(exc.__traceback__)
        where = " <- ".join(f"{f.name}:{f.lineno} {(f.line or '').strip()[:60]}" for f in frames[-3:])
        report.error = f"{type(exc).__name__}: {exc} [{where}]"
    # digests earned before a later failure are still worth checking, so the report goes back either way
    out.put(report)


def report_phase(phase: VariantPhase, total: PhaseResult) -> int:
    """Prints the verdict for one variant-rotating phase and returns how many FAIL lines it printed.

    A variant that did not complete either broke (FAIL), was refused by an operation the store may decline
    (DECLARED GAP), or was never reached because the phase ran out of time (FAIL: a scheduling bug here).
    """
    if declared_gap(phase.key):
        print(f"  DECLARED GAP {phase.key}: the whole phase was declared absent, so every shape in it is "
              f"untested here", file=sys.stderr)
        return 0

    name, ran, printed = phase.named(), sum(total.done.values()), 0
    print(f"{name}: {ran} completed across {len(total.done)} variants — "
          + ", ".join(f"{v}={n}" for v, n in sorted(total.done.items())))
    for variant, why in sorted(total.failures.items()):
        print(f"  FAIL {phase.key}/{variant}: {why}", file=sys.stderr)
        printed += 1
    # a phase whose variants were all refused did run; each refusal is reported as a gap below
    if not ran and not total.failures and len(total.refused) < len(total.done):
        print(f"  FAIL: the {name} phase did not run", file=sys.stderr)
        return printed + 1
    for variant, count in sorted(total.done.items()):
        if count or variant in total.failures:
            continue
        if variant in total.refused:
            print(f"  DECLARED GAP {phase.key}/{variant}: {total.refused[variant]} — so this shape is "
                  f"untested here", file=sys.stderr)
        else:
            print(f"  FAIL {phase.key}/{variant}: never reached — the phase ran out of budget before its "
                  f"turn, so it is untested here", file=sys.stderr)
            printed += 1
    return printed


# no appended block is anywhere near this; a header claiming more is not a header
MAX_FRAME_BODY = 16 << 20


def frame_at(data, offset: int):
    """The (writer, seq, length, crc_ok) of a well-formed frame at offset, or None."""
    if offset + HEADER.size > len(data):
        return None
    magic, writer, seq, length, crc = HEADER.unpack_from(data, offset)
    end = offset + HEADER.size + length
    if magic != MAGIC or length > MAX_FRAME_BODY or end > len(data):
        return None
    return writer, seq, length, binascii.crc32(data[offset + HEADER.size:end]) == crc


def next_frame(data, offset: int) -> int:
    """Offset of the next frame with a valid header and checksum after offset, or -1."""
    at = data.find(MAGIC, offset + 1)
    while at != -1:
        frame = frame_at(data, at)
        if frame and frame[3]:
            return at
        at = data.find(MAGIC, at + 1)
    return -1


def verify_shared(path: str, expected: dict[BlockId, int]) -> list[str]:
    """Walks the frames; after a broken one, resumes at the next valid frame so later blocks are still found."""
    # an absent file with nothing expected would compare 0 == 0 and pass
    if not expected:
        return ["no writer acknowledged a single shared block — the concurrent-append phase did not run"]
    if not os.path.exists(path):
        return [f"{len(expected)} blocks were acknowledged but the file does not exist"]

    problems, seen, broken = [], set(), 0
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
        try:
            offset = 0
            while offset < len(data):
                frame = frame_at(data, offset)
                if frame is None:
                    resume = next_frame(data, offset)
                    stop = resume if resume != -1 else len(data)
                    problems.append(f"offset {offset}: block frame broken — an append landed where it should not; "
                                    f"{stop - offset} bytes up to offset {stop} are not whole blocks")
                    broken += stop - offset
                    offset = stop
                    continue
                writer, seq, length, crc_ok = frame
                if not crc_ok:
                    problems.append(f"offset {offset}: block (writer {writer} seq {seq}) content is wrong")
                if BlockId(writer, seq) in seen:
                    problems.append(f"offset {offset}: block (writer {writer} seq {seq}) appears twice")
                seen.add(BlockId(writer, seq))
                offset += HEADER.size + length
        finally:
            if isinstance(data, mmap.mmap):
                data.close()
    missing = [(block_id, length) for block_id, length in expected.items() if block_id not in seen]
    where = (f"; {broken} bytes of the file are not whole blocks, so it may be there torn rather than lost"
             if broken else "")
    for block_id, length in missing:
        problems.append(f"block (writer {block_id.writer} seq {block_id.seq}, {length} bytes) was "
                        f"acknowledged but is not in the file as a whole block{where}")
    return problems


# passed to workers through the environment: on macOS they are spawned, re-import this module, and would
# never see globals set in main()
_MOUNT_ROOT = os.environ.get("FSHAMMER_MOUNT_ROOT") or None
_ALT_ROOT = os.environ.get("FSHAMMER_ALT_ROOT") or None
_SERVER_ROOT = os.environ.get("FSHAMMER_SERVER_ROOT") or None
_SINGLE_WRITER_HANDLE = os.environ.get("FSHAMMER_ONE_WRITE_HANDLE") == "1"
_NO_MAP = os.environ.get("FSHAMMER_NO_MAP") == "1"
_NO_LOCK = os.environ.get("FSHAMMER_NO_LOCK") == "1"
# `phase` or `phase/variant` names from --declare-gap; through the environment like the flags above, and
# also assigned in main() because the parent's verdict needs them
_DECLARED_GAPS = frozenset(g for g in os.environ.get("FSHAMMER_DECLARED_GAPS", "").split(",") if g)


# phases that are not variant rotations but can still be declared. `shared` cannot work on a store that
# PUTs whole files on close (the last close wins), and no errno says so.
DECLARABLE_PHASES = frozenset({"shared"})


def declared_gap(phase: str, variant: "str | None" = None) -> bool:
    """Whether this phase, or one variant of it, was declared absent with --declare-gap."""
    return phase in _DECLARED_GAPS or (variant is not None and f"{phase}/{variant}" in _DECLARED_GAPS)


# most the ENOSPC check will write; with more free space than this it reports a gap instead
FILL_BUDGET = 2 << 30


def server_size_verdict(path: str) -> str:
    """The file's length as the server stores it, read without the mount, to tell a storage loss from a
    client reporting the wrong size."""
    if not _SERVER_ROOT or not _MOUNT_ROOT:
        return " (no --server-root, so nobody asked the server whether IT has the bytes)"
    try:
        mirror = os.path.join(_SERVER_ROOT, os.path.relpath(path, _MOUNT_ROOT))
        return f" — the SERVER's own copy is {os.path.getsize(mirror)} bytes"
    except OSError as exc:
        return f" — the server's copy could not be read: {exc}"


def server_verdict(path: str, model: bytearray, off: int, want: bytes, bad: int) -> str:
    """What the SERVER holds at the divergent byte, read without going through the mount."""
    if not _SERVER_ROOT or not _MOUNT_ROOT:
        return "not available (no --server-root)"
    try:
        mirror = os.path.join(_SERVER_ROOT, os.path.relpath(path, _MOUNT_ROOT))
        with open(mirror, "rb") as fh:
            theirs = fh.read()
    except OSError as exc:
        return f"could not read the server's copy: {exc}"
    at = bad - off
    if theirs[off:off + len(want)] == want:
        return (f"CORRECT on the server (its file is {len(theirs)} bytes) — the bytes were stored and the "
                f"client read something else, so nothing was lost")

    # the run of zeros around the bad byte names the operation that dropped it: a hole on an operation's
    # boundaries points at that operation. Zeros, not bytes differing from the model, because the
    # pseudo-random model matches a zero one byte in 256 and would cut the run short.
    lo = bad
    while lo > 0 and theirs[lo - 1] == 0:
        lo -= 1
    hi = bad
    while hi < len(theirs) and theirs[hi] == 0:
        hi += 1
    # zeros where the model also has zeros prove nothing
    owed = sum(1 for i in range(lo, min(hi, len(model))) if model[i])
    return (f"WRONG on the server too (its file is {len(theirs)} bytes, model {len(model)}): "
            f"server={theirs[bad:bad + 16]!r} want={want[at:at + 16]!r} — the bytes never reached storage. "
            f"The hole of zeros runs {lo}..{hi} ({hi - lo} bytes), of which {owed} should be non-zero")


def check_enospc(d: str, result: PhaseResult) -> None:
    """malice/enospc_is_honest, run by the parent after every writer has finished, so a full disk is not
    charged to whatever else was running."""
    variant = "enospc_is_honest"
    try:
        os.makedirs(d, exist_ok=True)
        run_enospc(d, result, variant)
    except Exception as exc:                              # noqa: BLE001 — filed under the variant
        result.record(variant, exc)
    else:
        if variant not in result.refused:
            result.done[variant] += 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_enospc(d: str, result: PhaseResult, variant: str) -> None:
    # a full filesystem may refuse a write with ENOSPC, or accept it and keep it; it must not accept it and
    # keep less. Bounded by FILL_BUDGET so a large filesystem is never filled.
    free = os.statvfs(d)
    free_bytes = free.f_bavail * free.f_frsize
    if free_bytes > FILL_BUDGET:
        result.refused[variant] = (
            f"{free_bytes >> 20} MiB free, over the {FILL_BUDGET >> 20} MiB fill budget — point "
            f"this at a smaller filesystem, or raise --fill-budget, to exercise a full disk")
        return

    # written and verified before the fill, to check the full disk does not damage existing data
    canary = os.path.join(d, "enospc-canary.bin")
    canary_body = payload(b"canary", 64 * 1024)
    write_whole(canary, canary_body)
    if contents(canary) != canary_body:
        raise AssertionError(f"{canary}: did not read back before the disk was even filled")

    filler = os.path.join(d, "enospc-filler.bin")
    chunk = payload(b"fill", 4 << 20)
    accepted, hit = 0, None
    fd = os.open(filler, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        while accepted < FILL_BUDGET + (64 << 20):
            try:
                wrote = os.write(fd, chunk)
            except OSError as exc:
                hit = exc.errno
                break
            if wrote == 0:
                hit = 0                      # accepted nothing and reported no error
                break
            accepted += wrote
            if wrote < len(chunk):
                # a short write is how a full disk usually arrives; the next write must then fail
                continue
        try:
            os.fsync(fd)
        except OSError as exc:
            if hit is None:
                hit = exc.errno              # deferred to the flush, which is still honest
    finally:
        os.close(fd)

    try:
        if hit is None:
            result.refused[variant] = (
                f"wrote {accepted >> 20} MiB without filling the filesystem — it is larger than "
                f"statvfs said, or thin-provisioned, so a full disk was never reached")
        else:
            if hit not in (errno.ENOSPC, errno.EDQUOT):
                raise AssertionError(
                    f"{filler}: the filesystem stopped accepting writes with "
                    f"{errno.errorcode.get(hit, hit)} rather than ENOSPC — a caller cannot tell "
                    f"a full disk from a broken one, and every retry loop in the world keys on "
                    f"that errno")

            # everything write(2) accepted must be in the file
            landed = os.stat(filler).st_size
            if landed != accepted:
                raise AssertionError(
                    f"{filler}: {accepted} bytes were accepted by write(2) and the file is "
                    f"{landed} ({landed - accepted:+d}). A full disk that TRUNCATES what it "
                    f"already took is the worst of both answers — the caller was told those "
                    f"bytes landed")

            if contents(canary) != canary_body:
                raise AssertionError(
                    f"{canary}: a file written and verified BEFORE the disk filled no longer "
                    f"reads back. Running out of space damaged data that was already safe")
    finally:
        os.unlink(filler)

    # and writes must succeed again once space is freed
    after = payload(b"after", 64 * 1024)
    recovered = os.path.join(d, "enospc-recovered.bin")
    write_whole(recovered, after)
    if contents(recovered) != after:
        raise AssertionError(
            f"{recovered}: the filesystem does not accept writes again after the space was "
            f"freed — a full disk you cannot recover from by deleting something")
    os.unlink(recovered)
    os.unlink(canary)


def fsync_path(path: str) -> None:
    """fsync, or F_FULLFSYNC where it exists: on macOS a plain fsync does not reach stable storage."""
    fd = os.open(path, os.O_RDONLY)
    try:
        if fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                return
            except OSError:
                pass                    # not every filesystem implements it
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_tree(path: str) -> None:
    """Removes a file, link or directory tree, restoring directory permissions a variant took away first."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    os.chmod(path, 0o700)
    for root, dirs, _ in os.walk(path):
        for d in dirs:
            if not os.path.islink(os.path.join(root, d)):
                os.chmod(os.path.join(root, d), 0o700)
    shutil.rmtree(path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit 0 = no FAIL lines. Each DECLARED GAP line on stderr is something the run did not test.\n"
               "exit 1 = at least one FAIL line on stderr, or every writer died without reporting.\n"
               "exit 2 = invalid invocation: bad arguments, a non-empty --dir, or an --alt-dir that is not\n"
               "the same directory as --dir.\n\n"
               "Disk use: about 1.5 GB at once with the defaults (plus 4 GiB on a filesystem without sparse\n"
               "files and at least 5 GiB free). If less than --fill-budget is free, it writes until writes are\n"
               "refused, and other programs on that filesystem get 'No space left on device' until it deletes\n"
               "its filler. Files are deleted after a run with no failures; a failed or interrupted run leaves\n"
               "them.")
    ap.add_argument("--version", action="version", version=f"fshammer {__version__}")
    ap.add_argument("--dir", required=True, help="directory to run in. Must be empty or absent (lost+found is ignored). After a run "
                         "with no failures, every entry in it except lost+found is deleted; see --keep.")
    ap.add_argument("--fill-budget", type=int, default=None, metavar="BYTES",
                    help="most the ENOSPC check writes, once, after every writer has finished (default 2 GiB, "
                         "plus up to 64 MiB). It runs only when statvfs reports less free space than this, so "
                         "a mount that under-reports free space will be written to.")
    ap.add_argument("--pre-audit-command", default=None, metavar="CMD",
                    help="shell command run once after every writer has finished and before the first "
                         "verification read, to make the filesystem misbehave under the verification itself "
                         "(revoke access, remove the export, return errors). A non-zero exit is a FAIL.")
    ap.add_argument("--crash-command", default=None, metavar="CMD",
                    help="shell command that makes the store lose anything it has not made durable: kill and "
                         "restart the server, or force-unmount and remount. It must leave the filesystem mounted "
                         "at the same path when it returns. Run once after "
                         "verification; every own-file and download, and their directories, are fsynced first "
                         "and re-read and hashed after. Skipped if the run already failed.")
    # separate processes on one mount share one client session; a second mount is a second session
    ap.add_argument("--alt-dir", default=None,
                    help="the same directory through a second mount. Odd-numbered writers append to shared.bin "
                         "and watch observed.log through it, and the cross-mount lock and publish checks use it. "
                         "Two NFS mounts of one server share one NFS client even with nosharecache, so the "
                         "server sees a single client.")
    # reads the server's own copy, around the client cache. Only valid for a store that applies a write
    # before acknowledging it: the publish phase reads here right after its rename, which a correct
    # write-behind store will not have applied yet.
    ap.add_argument("--server-root", default=None,
                    help="where the server stores these files, if readable from this host. Distinguishes a "
                         "storage loss from a stale client cache. Only for a store where a file fsynced through "
                         "the mount, with its directory fsynced, is immediately visible here with that content; "
                         "a store that acknowledges fsync before applying it fails the publish checks.")
    # for writeback-cache clients; see POSITIONAL_KINDS and hammer_own
    ap.add_argument("--single-writer-handle", action="store_true",
                    help="one write descriptor per own-file, and pwrite at end-of-file instead of "
                         "O_APPEND for positional kinds, for clients that cache writes. Reported as a gap.")
    # mmap is how databases and loaders read, so skipping it must be asked for and is reported as a gap
    ap.add_argument("--no-map", action="store_true",
                    help="skip the mmap variants. Reported as a gap.")
    # likewise for locking
    ap.add_argument("--no-lock", action="store_true",
                    help="skip the fcntl locking phase. Reported as a gap.")
    # for shapes a filesystem cannot be asked about: a whole phase when the subject is the mount itself
    # (invented inode numbers), or one variant that is impossible by construction with no errno to say so
    # (many appenders to one file on a store that PUTs on close). Not for variants that fail.
    # --max-name-length moves the length limit instead of dropping the name variants, so every shape still runs
    ap.add_argument("--max-name-length", type=int, default=255, metavar="BYTES",
                    help="longest path component to create, 16 to 255 (default 255). For stores that reserve "
                         "part of a name, such as a WebDAV client that caches entries as local files with a "
                         "suffix. Length checks still run, at this limit; the difference is reported as a gap.")
    ap.add_argument("--declare-gap", action="append", default=[], metavar="PHASE[/VARIANT]",
                    help="skip a phase or one variant of it, reported as a gap. Repeatable. Phases: "
                         + ", ".join(sorted({phase.key for phase in VARIANT_PHASES} | DECLARABLE_PHASES))
                         + ". Variant names are the name=count entries on each phase's output line. An unknown "
                         "phase is rejected; a variant name that matches nothing is a FAIL.")
    ap.add_argument("--keep", action="store_true",
                    help="keep the files after a run with no failures. After a failed run they are always kept.")
    ap.add_argument("--duration", type=int, default=300,
                    help="total run time in seconds, split between phases in fixed shares (default 300). The "
                         "first pass of every variant always completes, even past this. 0 = one pass. A run "
                         "can end early if shared.bin reaches its size cap.")
    ap.add_argument("--procs", type=int, default=4,
                    help="concurrent writer processes (default 4). With 1, the contended variants and the "
                         "cross-writer checks are reported as gaps.")
    ap.add_argument("--seed", type=int, default=1,
                    help="seed for the random choices (default 1). With --duration 0 and the same --procs, each "
                         "writer makes the same choices; how many operations run, and their interleaving, "
                         "still depend on timing.")
    args = ap.parse_args()

    global _MOUNT_ROOT, _ALT_ROOT, _SERVER_ROOT, _SINGLE_WRITER_HANDLE, _NO_MAP, _NO_LOCK, _DECLARED_GAPS
    global MAX_NAME_LENGTH
    _MOUNT_ROOT, _ALT_ROOT, _SERVER_ROOT = args.dir, args.alt_dir, args.server_root
    _SINGLE_WRITER_HANDLE = args.single_writer_handle
    _NO_MAP = args.no_map
    _NO_LOCK = args.no_lock
    # 16 is the shortest name long_names can build; nothing POSIX accepts more than 255
    if args.procs < 1:
        ap.error(f"--procs {args.procs}: must be at least 1")
    if args.duration < 0:
        ap.error(f"--duration {args.duration}: must not be negative")
    if args.fill_budget is not None and args.fill_budget <= 0:
        ap.error(f"--fill-budget {args.fill_budget}: must be positive")
    if not 16 <= args.max_name_length <= NAME_MAX:
        ap.error(f"--max-name-length {args.max_name_length}: must be between 16 and {NAME_MAX} (NAME_MAX)")
    MAX_NAME_LENGTH = args.max_name_length

    # only phase names can be checked up front; variant names are checked after the run
    known = {phase.key for phase in VARIANT_PHASES} | DECLARABLE_PHASES
    for declaration in args.declare_gap:
        if declaration.split("/", 1)[0] not in known:
            ap.error(f"--declare-gap {declaration}: no phase called '{declaration.split('/', 1)[0]}'. "
                     f"The phases are {', '.join(sorted(known))}.")
    _DECLARED_GAPS = frozenset(args.declare_gap)
    # own-files are named by writer and iteration, so a previous run's leftovers would be appended to and
    # reported as the filesystem's fault
    try:
        leftovers = sorted(set(os.listdir(args.dir)) - {"lost+found"}) if os.path.isdir(args.dir) else []
    except OSError as exc:
        return cannot_start(f"--dir {args.dir} could not be listed", exc)
    if leftovers:
        ap.error(f"--dir {args.dir}: must be empty or absent, and holds {len(leftovers)} entr"
                 f"{'y' if len(leftovers) == 1 else 'ies'} (first: {leftovers[0]!r})")
    # ... and again where a spawned worker will find them.
    os.environ["FSHAMMER_MOUNT_ROOT"] = args.dir
    os.environ["FSHAMMER_ALT_ROOT"] = args.alt_dir or ""
    os.environ["FSHAMMER_SERVER_ROOT"] = args.server_root or ""
    os.environ["FSHAMMER_ONE_WRITE_HANDLE"] = "1" if args.single_writer_handle else ""
    os.environ["FSHAMMER_DECLARED_GAPS"] = ",".join(sorted(args.declare_gap))
    os.environ["FSHAMMER_NO_MAP"] = "1" if args.no_map else ""
    os.environ["FSHAMMER_NO_LOCK"] = "1" if args.no_lock else ""
    os.environ["FSHAMMER_MAX_NAME_LENGTH"] = str(args.max_name_length)
    if args.fill_budget is not None:
        globals()["FILL_BUDGET"] = args.fill_budget
    if args.alt_dir:
        if args.procs < 2:
            usage_error("--alt-dir needs --procs 2 or more: only odd-numbered writers use it")
        if os.path.realpath(args.alt_dir) == os.path.realpath(args.dir):
            usage_error("--alt-dir is the same path as --dir; it must be the same directory through a second mount")

    try:
        os.makedirs(args.dir, exist_ok=True)
        # before the workload, so nothing this run created can answer the probe
        cap_was_unnecessary = MAX_NAME_LENGTH < NAME_MAX and probe_the_cap(args.dir)
        if args.alt_dir:
            make_alt_dir(args.alt_dir)
            require_same_export(args.dir, args.alt_dir)
    except OSError as exc:
        return cannot_start("--dir or --alt-dir could not be set up", exc)

    def shared_dir_for(w: int) -> str:
        """Odd writers reach shared.bin through --alt-dir; per-writer files stay on --dir."""
        return args.alt_dir if (args.alt_dir and w % 2) else args.dir
    started_at = time.monotonic()
    deadline = started_at + args.duration
    results: Queue = Queue()
    procs = [Process(target=worker, args=(args.dir, shared_dir_for(w), w, deadline, args.seed, args.procs, results))
             for w in range(args.procs)]
    for p in procs:
        p.start()

    failures, shared_expected, files_checked, total_bytes = 0, {}, 0, 0
    # counted separately so an unrelated failure does not mark the own-file hashes as mismatched
    own_file_mismatches = 0
    downloads_checked, downloads_run, downloaded_bytes = 0, 0, 0
    flavour_total: dict[str, int] = {}
    totals = {phase.key: PhaseResult.over(()) for phase in VARIANT_PHASES}
    observed_expected: dict[BlockId, int] = {}
    observer_total: dict[str, int] = {}
    open_refusals: dict[int, int] = {}
    lock_increments = 0
    sidecars_ignored = 0
    # re-checked after --crash-command
    durable_files: list = []

    def next_report(outstanding: int) -> WriterReport:
        """One writer's report, printing progress while waiting so a dead or slow writer is visible."""
        while True:
            try:
                return results.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                alive = sum(1 for p in procs if p.is_alive())
                print(f"  parent {int(time.monotonic() - started_at)}s: {outstanding} report(s) outstanding, "
                      f"{alive} writer(s) alive", file=sys.stderr, flush=True)
                if alive == 0:
                    raise SystemExit(
                        f"FAIL: every writer exited without reporting — {outstanding} report(s) never arrived. "
                        f"A writer that dies before its put() takes its whole share of the oracle with it, "
                        f"so this is a FAILED run and not an empty one.")

    # collect every report before auditing, so no verification read races a writer still running
    reports = []
    for remaining in range(len(procs), 0, -1):
        reports.append(next_report(remaining))
    for p in procs:
        p.join()

    # the disk is filled only now, with nothing else writing, and the audit below then reads everything back
    if not declared_gap("malice"):
        enospc = PhaseResult.over(("enospc_is_honest",))
        if declared_gap("malice", "enospc_is_honest"):
            enospc.refused["enospc_is_honest"] = "--declare-gap"
        else:
            check_enospc(os.path.join(args.dir, "enospc"), enospc)
        totals["malice"].absorb(enospc)

    if args.pre_audit_command:
        # after every writer is joined and before the first verification read; a failure is reported and
        # the audit carries on, since how the audit copes is what this hook is for
        print(f"pre-audit: running {args.pre_audit_command!r}", flush=True)
        pre = subprocess.run(args.pre_audit_command, shell=True, capture_output=True, text=True)
        if pre.returncode != 0:
            print(f"  FAIL pre-audit command exited {pre.returncode}: {pre.stderr.strip() or pre.stdout.strip()}",
                  file=sys.stderr)
            failures += 1

    for report in reports:
        if report.error:
            print(f"  FAIL writer {report.writer}: {report.error}", file=sys.stderr)
            failures += 1
        for path, model in sorted(report.digests.items()):
            durable_files.append((path, model.digest, model.size, "own file"))
            files_checked += 1
            total_bytes += model.size
            # contents() drops the page cache first, so the hash is not answered from the writer's own pages
            actual = audited_contents(path, "own file")
            if actual is None:
                failures += 1
                own_file_mismatches += 1
            elif hashlib.sha256(actual).hexdigest() != model.digest:
                print(f"  FAIL {path}: sha256 does not match the model — file {len(actual)} bytes, "
                      f"model {model.size} ({len(actual) - model.size:+d})", file=sys.stderr)
                failures += 1
                own_file_mismatches += 1
        for path, landed in sorted(report.downloads.items()):
            durable_files.append((path, landed.digest, landed.size, "download"))
            downloads_checked += 1
            downloads_run += landed.runs
            downloaded_bytes += landed.size
            actual = audited_contents(path, "download")
            if actual is None:
                failures += 1
            elif hashlib.sha256(actual).hexdigest() != landed.digest:
                print(f"  FAIL {path}: the downloaded bytes do not match the model — file {len(actual)} "
                      f"bytes, model {landed.size} ({len(actual) - landed.size:+d})", file=sys.stderr)
                failures += 1
            if landed.part and os.path.exists(landed.part):
                print(f"  FAIL {landed.part}: the .part file outlived the download that wrote it", file=sys.stderr)
                failures += 1
        for flavour, count in report.download_flavours.items():
            flavour_total[flavour] = flavour_total.get(flavour, 0) + count
        shared_expected.update({BlockId(report.writer, b.seq): b.length for b in report.shared})
        observed_expected.update({BlockId(report.writer, b.seq): b.length for b in report.observed})
        for kind, count in report.observer.items():
            observer_total[kind] = observer_total.get(kind, 0) + count
        for code, count in report.open_refusals.items():
            open_refusals[code] = open_refusals.get(code, 0) + count
        lock_increments += report.lock_increments
        sidecars_ignored += report.sidecars_ignored
        for key, result in report.phases.items():
            totals[key].absorb(result)

    print(f"own files: {files_checked} checked, {total_bytes} bytes, "
          f"{'all sha256 match' if not own_file_mismatches else 'MISMATCHES ABOVE'}")

    # an absent phase must not read like a clean one: nothing to check reads as nothing wrong
    print(f"downloads: {downloads_run} completed as a real downloader does, {downloads_checked} final files "
          f"re-checked here, {downloaded_bytes} bytes — "
          + ", ".join(f"{f}={n}" for f, n in sorted(flavour_total.items())))
    if not downloads_checked:
        print("  FAIL: no writer finished a single download — the download phase did not run", file=sys.stderr)
        failures += 1
    else:
        # a flavour that never completed is a hole, so it is reported
        for flavour in DOWNLOAD_FLAVOURS:
            if not flavour_total.get(flavour):
                print(f"  FAIL downloads/{flavour}: never completed — that download shape is untested here",
                      file=sys.stderr)
                failures += 1

    if sidecars_ignored:
        # say what the listing checks excluded
        print(f"  note: {sidecars_ignored} client-generated sidecar(s) were excluded from listing checks "
              f"(AppleDouble `._name`, silly-rename `.nfsXXXX`). Those are the CLIENT's bookkeeping: it "
              f"creates them without being asked and removes them on its own schedule, so a server is not "
              f"answerable for them.", file=sys.stderr)
    # a misspelled variant excuses nothing while the operator believes it did, so it is a failure
    unmatched = [d for d in sorted(_DECLARED_GAPS) if "/" in d
                 and d.split("/", 1)[1] not in totals.get(d.split("/", 1)[0], PhaseResult.over(())).done]
    for declaration in unmatched:
        phase_key = declaration.split("/", 1)[0]
        print(f"  FAIL --declare-gap {declaration}: the {phase_key} phase has no such variant, so the "
              f"declaration excused nothing and this run tested something other than what it claims",
              file=sys.stderr)
    failures += len(unmatched)

    if MAX_NAME_LENGTH < NAME_MAX:
        print(f"  DECLARED GAP: --max-name-length {MAX_NAME_LENGTH}, so apart from one probe "
              f"before the run, no name between {MAX_NAME_LENGTH + 2} and {NAME_MAX} bytes was written. Every length-sensitive shape still "
              f"ran at {MAX_NAME_LENGTH} — a name exactly at the limit, and one byte past it that had to be "
              f"refused rather than trimmed — but NAME_MAX itself went untested, and a store that fails "
              f"only in the last few bytes of a name would not be caught by this run.", file=sys.stderr)
        if cap_was_unnecessary:
            print(f"  DECLARED GAP CLOSED: and the cap was not needed. A {NAME_MAX}-byte name round-tripped "
                  f"here before the workload started, so --max-name-length {MAX_NAME_LENGTH} is costing the "
                  f"coverage above for nothing — drop it from this invocation.", file=sys.stderr)
        else:
            print(f"  ...and the cap is EARNED: this run probed a {NAME_MAX}-byte name before starting and "
                  f"the filesystem would not hold it.", file=sys.stderr)
    if _NO_MAP:
        print("  DECLARED GAP: --no-map, so nothing here mapped a file. Mapping is how a database reads and "
              "how a page reaches a server with nobody calling write(2); this run did not test any of it.",
              file=sys.stderr)
    if not CAN_DROP_CACHE:
        print("  DECLARED GAP: this platform has no posix_fadvise, so no verification read could be forced "
              "past the client's page cache. Every read-back in this run — including the final SHA-256 of "
              "each own-file, which is the headline oracle — may have been answered out of the pages the "
              "writer had just filled, and would agree with itself whatever the server actually stored.",
              file=sys.stderr)
    if args.procs < 2:
        print("  DECLARED GAP: --procs 1, so no writer ever followed another writer's published pointer, "
              "and shared.bin had one appender. A name becoming visible before the bytes it refers to, and "
              "appends from several processes interleaving, need two writers.", file=sys.stderr)
    if args.single_writer_handle:
        print("  DECLARED GAP: --single-writer-handle, so own-files were written through one descriptor each, "
              "and positional kinds used pwrite at end-of-file instead of O_APPEND. Several descriptors "
              "writing one file were not tested.", file=sys.stderr)
    if _NO_LOCK:
        print("  DECLARED GAP: --no-lock, so not one fcntl advisory lock was taken in this run. Locking is "
              "the only contract here whose whole value is the REFUSAL — a store that grants every request "
              "passes every other oracle in this tool — and none of it was tested.", file=sys.stderr)
    elif fcntl is None or not FLOCK:
        print(f"  DECLARED GAP: no usable struct flock on {sys.platform}, so the locking phase did not run. "
              f"Guessing the layout would risk reporting a failure that belongs to this tool.", file=sys.stderr)
    elif not lock_increments:
        print("  FAIL locks/exclusive_rmw: no writer completed a single locked increment, so the phase "
              "that proves this filesystem arbitrates at all did not run", file=sys.stderr)
        failures += 1
    else:
        # every increment was made under an exclusive lock, so the total is exact; short means two writers
        # held the range at once
        held = struct.unpack("<Q", read_plainly(os.path.join(args.dir, LOCK_DIR, LOCK_COUNTER))[:8])[0]
        if held < lock_increments:
            print(f"  FAIL locks/exclusive_rmw: {lock_increments} increments were each taken under an "
                  f"exclusive lock, but the counter holds {held} — {lock_increments - held} update(s) were "
                  f"LOST, so two writers held the same range at the same time", file=sys.stderr)
            failures += 1
        elif held > lock_increments:
            print(f"  FAIL locks/exclusive_rmw: the counter holds {held} but only {lock_increments} "
                  f"increments were made — {held - lock_increments} update(s) this run never wrote, so the "
                  f"file is not the one the writers were locking", file=sys.stderr)
            failures += 1
        else:
            print(f"locks: {lock_increments} read-modify-writes under an exclusive lock, and the counter "
                  f"holds exactly that")
    options = mount_options(args.dir)
    if not _NO_LOCK and ("nolock" in options or "local_lock=all" in options):
        print(f"  DECLARED GAP: this mount carries nolock, so every fcntl lock in this run — the locking "
              f"phase's, and the ones a SQLite-shaped commit implies — was arbitrated by the CLIENT and the "
              f"server never saw one. Processes on THIS machine still exclude each other correctly, which "
              f"is what the locking phase above just proved and is enough for every user of one machine. "
              f"What went untested is two CLIENTS, where the server is the only thing that could arbitrate "
              f"and a server without a lock manager cannot. ({options})", file=sys.stderr)
    if args.alt_dir and os.stat(args.dir).st_dev == os.stat(args.alt_dir).st_dev:
        print("  DECLARED GAP: --dir and --alt-dir report the same st_dev, so they are probably one client "
              "instance (a bind mount, or NFS without nosharecache). The cross-mount checks then ran "
              "against that one client, not two.", file=sys.stderr)
    if not args.alt_dir:
        print("  DECLARED GAP: no --alt-dir, so every read in this run went through the one mount that "
              "wrote it. A client cannot serve itself a stale name it invented, so the cross-session half "
              "of the ordering oracles did not run.", file=sys.stderr)
    # always printed: a syscall cannot say how its answer was produced
    print("  DECLARED GAP: nothing here can tell an answer served from an INDEX from one the server "
          "rescanned to produce. Both return the same bytes, and the only difference is how long they took "
          "— which to a syscall is a slow filesystem, and there is no oracle for slow in this tool. So a "
          "change that leaves every answer correct and makes each one O(N) passes this suite green. If that "
          "is the property under test, it needs measuring where the work happens, not here.", file=sys.stderr)
    for phase in VARIANT_PHASES:
        failures += report_phase(phase, totals[phase.key])

    # read through --dir even when some writers used --alt-dir: it is one file on the server.
    # observed.log was appended by one writer while the others only looked, and uses the same frames
    observed_path = os.path.join(args.dir, OBSERVED)
    watchers = sum(n for k, n in observer_total.items() if k != "appended")
    if not observer_total.get("appended"):
        print("  FAIL observer: nothing was appended — the phase did not run", file=sys.stderr)
        failures += 1

    # the frames are always verified; how much watching happened is reported beside that
    for problem in verify_shared(observed_path, observed_expected):
        print(f"  FAIL {OBSERVED}: {problem}", file=sys.stderr)
        failures += 1

    if observer_total.get("appended") and watchers < max(20, observer_total["appended"] // 200):
        # too few observations to have overlapped the writing is a gap, not a pass
        print(f"  DECLARED GAP observer: {observer_total['appended']} blocks appended but only {watchers} "
              f"observations overlapped them — too thin to be evidence, so a stat racing an append is "
              f"untested here (needs --procs 2 or more, and a mount that can answer a stat)", file=sys.stderr)
    obs_acked = sum(HEADER.size + n for n in observed_expected.values())
    obs_size = os.path.getsize(observed_path) if os.path.exists(observed_path) else 0
    print(f"{OBSERVED}: {len(observed_expected)} blocks appended by 1 writer, {obs_acked} bytes acknowledged, "
          f"{obs_size} on disk, while {watchers} observations ran — "
          + ", ".join(f"{k}={n}" for k, n in sorted(observer_total.items()) if k != "appended"))

    shared_path = os.path.join(args.dir, SHARED)
    if declared_gap("shared"):
        # without this, "0 blocks from 0 writers" would read like a clean run
        print("  DECLARED GAP shared: many writers appending to ONE file was declared absent, so the "
              "shape most likely to lose appended data is untested here", file=sys.stderr)
    else:
        problems = verify_shared(shared_path, shared_expected)
        acked = sum(HEADER.size + n for n in shared_expected.values())
        size = os.path.getsize(shared_path) if os.path.exists(shared_path) else 0
        # writers that contributed, not writers that were started
        contributors = {b.writer for b in shared_expected}
        via_alt = sorted(w for w in contributors if shared_dir_for(w) != args.dir)
        print(f"{SHARED}: {len(shared_expected)} blocks from {len(contributors)} writers "
              f"({len(contributors) - len(via_alt)} via --dir, {len(via_alt)} via --alt-dir), "
              f"{acked} bytes acknowledged, {size} on disk")
        if open_refusals:
            named = ", ".join(f"{errno.errorcode.get(c, c)}={n}" for c, n in sorted(open_refusals.items()))
            # on one mount O_CREAT without O_EXCL must open what is there; across two mounts nothing locks
            # both directories, and some servers refuse the same way
            if args.alt_dir:
                print(f"  retried opening {SHARED}: {named} — two mounts of one export race the walk-then-create, "
                      f"which a third-party server does too; counted, not charged to the server")
            else:
                problems.insert(0, f"opening it refused {named} on a SINGLE mount, where O_CREAT without O_EXCL "
                                   f"must open the file that is there")
        if size != acked:
            problems.insert(0, f"file is {size} bytes but {acked} were acknowledged ({size - acked:+d})")
        # --alt-dir with nothing routed through it must not read as a two-mount run
        if args.alt_dir and not via_alt:
            problems.insert(0, "--alt-dir was given but no writer appended through it — the two-mount case did not run")

        for problem in problems[:20]:
            print(f"  FAIL {shared_path}: {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  … and {len(problems) - 20} more", file=sys.stderr)
        failures += len(problems)

    # ── the crash ───────────────────────────────────────────────────────────────────────────────────
    # everything above ran against a store that never stopped. Make it lose what it had not made durable,
    # with a command only the caller can supply, then hash the own-files and downloads again.
    if args.crash_command:
        if failures:
            print("  skipping the crash: the run already failed, and a second failure afterwards could not "
                  "be attributed to durability", file=sys.stderr)
        else:
            # fsync every audited file and its directory first, so the audit asks only what POSIX promises
            for path, _, _, kind in durable_files:
                try:
                    fsync_path(path)
                except OSError as exc:
                    print(f"  FAIL {path}: fsync before the crash failed ({kind}): {exc}", file=sys.stderr)
                    failures += 1
            directories = {os.path.dirname(path) for path, *_ in durable_files}
            directories.add(os.path.dirname(os.path.abspath(args.dir)))     # --dir's own entry
            for directory in sorted(directories):
                try:
                    fsync_path(directory)
                except OSError as exc:
                    # some filesystems refuse fsync on a directory; that is not a durability failure
                    if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EISDIR):
                        print(f"  FAIL {directory}: directory fsync before the crash failed: {exc}", file=sys.stderr)
                        failures += 1
            if not failures:
                print(f"crash: running {args.crash_command!r}", flush=True)
                crashed = subprocess.run(args.crash_command, shell=True, capture_output=True, text=True)
                if crashed.returncode != 0:
                    print(f"  FAIL: the crash command exited {crashed.returncode} — the store was NOT made to "
                          f"lose anything, so nothing below this line is a durability result. "
                          f"{crashed.stderr.strip()[:400]}", file=sys.stderr)
                    failures += 1
                else:
                    lost, corrupt = [], []
                    for path, digest, size, kind in durable_files:
                        try:
                            actual = contents(path)
                        except OSError as exc:
                            lost.append(f"{path} ({kind}, {errno.errorcode.get(exc.errno, exc.errno)})")
                            continue
                        if hashlib.sha256(actual).hexdigest() != digest:
                            corrupt.append(f"{path} ({kind}, {len(actual)} bytes against {size})")

                    for path in lost:
                        print(f"  FAIL {path}: acknowledged, and gone after the crash", file=sys.stderr)
                    for path in corrupt:
                        print(f"  FAIL {path}: survived the crash holding different bytes", file=sys.stderr)
                    failures += len(lost) + len(corrupt)
                    print(f"durability: {len(durable_files)} acknowledged file(s) re-checked after the crash — "
                          + ("all still match" if not (lost or corrupt)
                             else f"{len(lost)} LOST, {len(corrupt)} CORRUPT"))

    # durability needs the caller's help, so a run without --crash-command says so
    if not args.crash_command:
        print("  DECLARED GAP: no --crash-command, so nothing made the store lose what it had not made "
              "durable. Every oracle above ran against a store that never stopped, and a write that was "
              "acknowledged while living only in a cache passes all of them. Pass a command that kills and "
              "restarts the thing under test to close this.", file=sys.stderr)

    elapsed = time.monotonic() - started_at
    if elapsed < args.duration:
        # a phase stopped on a byte cap rather than the clock
        print(f"note: finished in {elapsed:.0f}s, before --duration {args.duration} — a phase hit a "
              f"size cap rather than the clock")
    if not failures and not args.keep:
        for entry in os.listdir(args.dir):
            if entry != "lost+found":
                try:
                    remove_tree(os.path.join(args.dir, entry))
                except OSError as exc:
                    print(f"note: could not remove {os.path.join(args.dir, entry)}: {exc}", file=sys.stderr)
    print(f"fshammer: {'OK' if not failures else f'{failures} problem(s)'} "
          f"({elapsed:.0f}s elapsed, --duration {args.duration})"
          + ("" if not failures or args.keep else f" — files kept in {args.dir}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
