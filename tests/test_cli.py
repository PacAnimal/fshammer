"""What a run leaves behind, and what it refuses to start in."""
import subprocess
import sys

from conftest import HAMMER


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HAMMER), *args], capture_output=True, text=True, check=False)


def test_a_non_empty_dir_is_refused_before_anything_is_written(tmp_path):
    """Own-files are named by writer and iteration, so a previous run's leftovers would read as data loss."""
    (tmp_path / "leftover.bin").write_bytes(b"x")
    proc = run("--dir", str(tmp_path), "--duration", "0")

    assert proc.returncode == 2, proc.stderr
    assert "must be empty or absent" in proc.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["leftover.bin"], "the refused run wrote something"


def test_a_clean_run_removes_what_it_created(tmp_path):
    target = tmp_path / "run"
    proc = run("--dir", str(target), "--duration", "0", "--procs", "2")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert list(target.iterdir()) == [], "a clean run left files behind"


def test_keep_leaves_the_files_in_place(tmp_path):
    target = tmp_path / "run"
    proc = run("--dir", str(target), "--duration", "0", "--procs", "2", "--keep")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert any(target.iterdir()), "--keep removed the files"


def test_a_declared_variant_stays_skipped_when_the_run_repeats(tmp_path):
    """The first pass and the repeats must both honour a declaration."""
    proc = run("--dir", str(tmp_path / "run"), "--duration", "20", "--procs", "2",
               "--declare-gap", "malice/sparse_far_offset")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "sparse_far_offset=0" in proc.stdout, "a declared variant ran during the repeats"
    assert "DECLARED GAP malice/sparse_far_offset" in proc.stderr


def test_one_writer_reports_the_contended_variants_as_gaps(tmp_path):
    """With nobody to contend with, a contended variant proves nothing and must not count as completed."""
    proc = run("--dir", str(tmp_path / "run"), "--duration", "0", "--procs", "1")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "DECLARED GAP malice/contended_exclusive" in proc.stderr
    assert "DECLARED GAP malice/contended_tree" in proc.stderr


def test_invalid_invocations_exit_2(tmp_path):
    for args in (["--procs", "0"], ["--duration", "-1"], ["--alt-dir", str(tmp_path / "run"), "--procs", "2"],
                 ["--alt-dir", str(tmp_path / "elsewhere"), "--procs", "1"]):
        proc = run("--dir", str(tmp_path / "run"), *args)
        assert proc.returncode == 2, f"{args}: exit {proc.returncode}\n{proc.stderr[-1000:]}"


def test_alt_dir_waits_for_a_name_the_other_mount_has_not_seen_yet(monkeypatch, tmp_path):
    """mkdir answers EEXIST while stat still says absent, as through a mount caching the name as missing."""
    from conftest import fshammer

    looks = iter([False, False, True])

    def makedirs(path, exist_ok=False):
        raise FileExistsError(17, "File exists", path)

    monkeypatch.setattr(fshammer.os, "makedirs", makedirs)
    monkeypatch.setattr(fshammer.os.path, "isdir", lambda path: next(looks))
    monkeypatch.setattr(fshammer.time, "sleep", lambda seconds: None)
    listed = []
    monkeypatch.setattr(fshammer.os, "listdir", lambda path: listed.append(path) or [])

    fshammer.make_alt_dir(str(tmp_path / "alt"))

    # reading the parent is what makes an NFS client revalidate a name it has cached as absent
    assert listed and all(path == str(tmp_path) for path in listed), listed


def test_a_failure_in_the_parents_enospc_check_is_reported_not_raised(tmp_path, monkeypatch):
    """The check runs in the parent after the writers; an exception there must become a FAIL, not end the run."""
    from conftest import fshammer

    def refuse(*_args, **_kwargs):
        raise OSError(5, "Input/output error")

    result = fshammer.PhaseResult.over(("enospc_is_honest",))
    monkeypatch.setattr(fshammer.os, "makedirs", refuse)
    fshammer.check_enospc(str(tmp_path / "enospc"), result)

    assert "enospc_is_honest" in result.failures, "the error escaped instead of being recorded"


def test_a_directory_that_cannot_be_created_is_a_fail_not_a_traceback(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    proc = run("--dir", str(blocker / "run"), "--duration", "0")

    assert proc.returncode == 1, proc.stderr[-1000:]
    assert "FAIL setup:" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_an_internal_error_exits_3_and_an_interrupt_130(monkeypatch, capsys):
    """Neither may read as a verdict on the filesystem, which exits 0 or 1."""
    from conftest import fshammer

    def broken():
        raise RuntimeError("a fault inside fshammer")

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(fshammer, "hammer", broken)
    assert fshammer.main() == 3
    assert "internal error" in capsys.readouterr().err
    monkeypatch.setattr(fshammer, "hammer", interrupted)
    assert fshammer.main() == 130                   # 128 + SIGINT


def group_alive(pgid: int) -> bool:
    """Whether any process in the group is left: writers are spawned on macOS, so their command line does not
    name --dir and pgrep -f cannot find them."""
    import os
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def test_sigterm_to_the_parent_stops_every_writer(tmp_path):
    """A terminated parent must not leave its writer processes running."""
    import signal
    import time

    target = tmp_path / "run"
    proc = subprocess.Popen([sys.executable, str(HAMMER), "--dir", str(target), "--duration", "60", "--procs", "2"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(target.glob("w*")):
        time.sleep(0.2)
    time.sleep(2)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=60)

    assert proc.returncode == 128 + signal.SIGTERM, err[-2000:]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and group_alive(proc.pid):
        time.sleep(0.5)
    assert not group_alive(proc.pid), "writer processes still running after the parent exited"
    assert any(target.iterdir()), "an interrupted run must leave its files"


def test_only_one_writer_writes_past_4_gib(tmp_path):
    """On a filesystem without sparse files each such file takes 4 GiB, so writers must not do it at once."""
    proc = run("--dir", str(tmp_path / "run"), "--duration", "0", "--procs", "2")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "sparse_far_offset=2" not in proc.stdout, "both writers wrote past 4 GiB"
    assert "sparse_far_offset=1" in proc.stdout or "DECLARED GAP malice/sparse_far_offset" in proc.stderr


def test_writers_end_when_the_parent_is_killed(tmp_path):
    """SIGKILL cannot be handled, so the writers must notice the parent is gone and stop by themselves."""
    import signal
    import time

    target = tmp_path / "run"
    proc = subprocess.Popen([sys.executable, str(HAMMER), "--dir", str(target), "--duration", "120", "--procs", "2"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(target.glob("w*")):
        time.sleep(0.2)
    time.sleep(2)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and group_alive(proc.pid):
        time.sleep(0.5)
    alive = group_alive(proc.pid)
    if alive:
        import os
        os.killpg(proc.pid, signal.SIGKILL)
    assert not alive, "writer processes outlived their parent"
