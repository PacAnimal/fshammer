"""A failing close or fsync must report its own error, and whether the file or only the handle is gone.

POSIX releases a descriptor even when close fails, so closing it again in cleanup raises EBADF and
replaces the real error. Driven against the real download_once, not a copy of it.
"""
import os
import random
import tempfile

from conftest import fshammer

SENTINEL = "the server refused this close"


def drive_with_a_failing_close() -> BaseException | None:
    """Runs one download and makes the close of its .part descriptor fail, as on a soft mount."""
    real_open, real_close = os.open, os.close
    live: set[int] = set()
    state: dict[str, object] = {"target": None, "failed": False}

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        live.add(fd)
        if state["target"] is None and flags & os.O_TRUNC and flags & os.O_WRONLY:
            state["target"] = fd
        return fd

    def failing_close(fd: int) -> None:
        if fd not in live:
            # a descriptor this process already released is being closed
            # again, and that second close is where the EBADF came from.
            raise AssertionError(f"fd {fd} was closed twice — the cleanup closed a released descriptor")
        live.discard(fd)
        real_close(fd)                    # POSIX frees it whether or not we report success
        if fd == state["target"] and not state["failed"]:
            state["failed"] = True
            raise OSError(5, SENTINEL)

    os.open, os.close = tracking_open, failing_close
    try:
        with tempfile.TemporaryDirectory() as directory:
            download = fshammer.download_once(directory, os.path.join(directory, "download-0"),
                                              writer=0, seq=0, flavour="oneshot", rng=random.Random(1))
            try:
                while True:
                    next(download)
            except StopIteration:
                return None
            except BaseException as exc:   # noqa: BLE001 — the exception is the result
                return exc
    finally:
        os.open, os.close = real_open, real_close


def test_the_real_error_survives() -> None:
    outcome = drive_with_a_failing_close()

    assert outcome is not None, "the close never failed, so this proves nothing — the rig did not arm"
    assert not isinstance(outcome, AssertionError), str(outcome)
    assert isinstance(outcome, OSError), f"expected the close's own OSError, got {outcome!r}"
    assert outcome.errno != 9, \
        f"the real error was replaced by EBADF from closing a released descriptor: {outcome}"
    assert SENTINEL in str(outcome), f"some other error surfaced instead of the close's: {outcome}"
    print(f"  a failing close reports itself: {outcome}")


def test_a_failed_close_says_whether_the_file_survived() -> None:
    """A failed close must say whether the path still exists: ESTALE reads the same either way.

    Both directions are checked, so a message that always says STILL THERE cannot pass.
    """
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise OSError(116, "Stale file handle")

    # A file that IS there. The handle is what went, and nothing justifies that.
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "standing.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        born = os.lstat(path)
        os.close = failing_close
        try:
            fshammer.close_or_explain(fd, path, "a rigged close", (born.st_dev, born.st_ino))
            still = None
        except BaseException as exc:            # noqa: BLE001 — the message IS the assertion
            still = exc
        finally:
            os.close = real_close

    assert still is not None, "the rigged close did not fail, so this proves nothing — the rig did not arm"
    assert isinstance(still, OSError), f"expected the close's own OSError, explained, got {still!r}"
    assert still.errno == 116, f"the original errno must survive the explanation, got {still.errno}"
    assert "STILL THERE" in str(still), \
        f"a close that failed on a file that is plainly present did not say so: {still}"
    assert "HANDLE" in str(still), f"it did not name the handle as what was lost: {still}"
    print(f"  a failed close on a live file blames the handle: {str(still)[:110]}…")

    # The other direction, driven directly: a path that is gone must read as an HONEST refusal, or the
    # check above passes on a message that says "STILL THERE" unconditionally.
    with tempfile.TemporaryDirectory() as directory:
        absent = os.path.join(directory, "never-existed.bin")
        fd = os.open(os.path.join(directory, "real.bin"), os.O_WRONLY | os.O_CREAT, 0o644)
        os.close = failing_close
        try:
            fshammer.close_or_explain(fd, absent, "a rigged close")
            raise AssertionError("the rigged close did not fail")
        except OSError as exc:
            message = str(exc)
        finally:
            os.close = real_close

    assert "GONE" in message and "honest" in message, \
        f"a close that failed on a path that does not exist must read as an honest refusal: {message}"
    print(f"  a failed close on a vanished path reads as honest: {message[:110]}…")


def test_a_failed_fsync_says_whether_the_file_survived() -> None:
    """The same verdict for a failing fsync, which is where a lost handle often shows up first."""
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError(116, "Stale file handle")

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "held.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        born = os.lstat(path)
        os.fsync = failing_fsync
        try:
            fshammer.fsync_or_explain(fd, path, "held across an enumeration", (born.st_dev, born.st_ino))
            outcome = None
        except BaseException as exc:            # noqa: BLE001 — the message IS the assertion
            outcome = exc
        finally:
            os.fsync = real_fsync
            os.close(fd)

    assert outcome is not None, "the rigged fsync did not fail, so this proves nothing — the rig did not arm"
    assert isinstance(outcome, OSError), f"expected the fsync's own OSError, explained, got {outcome!r}"
    assert outcome.errno == 116, f"the original errno must survive the explanation, got {outcome.errno}"
    assert "STILL THERE" in str(outcome) and "HANDLE" in str(outcome), \
        f"a failed fsync on a live file must blame the handle: {outcome}"
    print(f"  a failed fsync on a live file blames the handle: {str(outcome)[:110]}…")

