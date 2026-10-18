"""fshammer against a filesystem nobody disputes, so a failure anywhere else means what it says.

An oracle is only worth what its own correctness is worth. A FAIL against the filesystem you are testing is
only a verdict on THAT filesystem if the tool is known to pass on one that is right — otherwise a variant
that broke in the tool and a volume that broke under it are the same red line. And the other direction
matters as much: an oracle nobody has seen FAIL is a hypothesis, so most tests here damage something on
purpose and assert the tool notices. For those, a zero exit is the failure.

These tests need a reference filesystem, named by environment variable:

    FSHAMMER_REFERENCE_DIR      an empty directory on a known-good POSIX filesystem (ext4 is the reference),
                                big enough to hold a sparse write past 4 GiB
    FSHAMMER_REFERENCE_ALT_DIR  the SAME filesystem through a second mount point (a bind mount), so the
                                cross-mount variants run. Optional; without it those variants declare a gap
                                and the full-coverage test is skipped
    FSHAMMER_SMALL_DIR          a small (~768 MiB) filesystem the ENOSPC check can actually fill. Optional

Run them as an ORDINARY user: root bypasses permission checks, so `mode_is_enforced` declines to run.
`scripts/reference-fs.sh` builds all three on Linux and prints the exports.
"""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import uuid

import pytest

from conftest import HAMMER

REFERENCE = os.environ.get("FSHAMMER_REFERENCE_DIR")
REFERENCE_ALT = os.environ.get("FSHAMMER_REFERENCE_ALT_DIR")
SMALL = os.environ.get("FSHAMMER_SMALL_DIR")

needs_reference = pytest.mark.skipif(not REFERENCE, reason="FSHAMMER_REFERENCE_DIR is not set (see scripts/reference-fs.sh)")

# variants the main reference filesystem cannot exercise, each covered by a test of its own
COVERED_ELSEWHERE = {"malice/enospc_is_honest"}

# "locks: 28 completed across 7 variants — close_drops=4, conflict_excludes=4, …"
PHASE_LINE = re.compile(r"^(?P<phase>[a-z-]+): \d+ completed across \d+ variants — (?P<counts>.+)$", re.M)


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, directory: pathlib.Path):
        self.exit, self.stdout, self.stderr, self.dir = proc.returncode, proc.stdout, proc.stderr, directory


def hammer(directory: pathlib.Path, *args: str, script: pathlib.Path = HAMMER) -> Run:
    """One mandatory pass and nothing more: these ask whether every variant RUNS and PASSES, not who wins a race."""
    proc = subprocess.run([sys.executable, str(script), "--dir", str(directory), "--duration", "0", *args],
                          capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return Run(proc, directory)


@pytest.fixture
def workdir():
    """A fresh directory on the reference filesystem, removed afterwards.

    Fresh, because the own-file oracle needs one: own-files are named from writer and iteration, so a second
    run over a first run's leavings appends to a file that already holds bytes the model never wrote.
    """
    path = pathlib.Path(REFERENCE) / f"run-{uuid.uuid4().hex}"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def script(tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nset -e\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def own_files(directory: pathlib.Path) -> str:
    """Own-files — the ones the audit re-reads — sit at the TOP of the run directory as `w{writer}-{n}-{kind}.bin`.

    -maxdepth 1 is load-bearing: phases keep subdirectories of files matching the same glob that nothing
    audits, so damaging one of those proves nothing.
    """
    return f"victims=$(find '{directory}' -maxdepth 1 -type f -name 'w*-*.bin' | sort)\n" \
           "count=$(printf '%s\\n' \"$victims\" | grep -c .)\n"


def zero_variants(stdout: str) -> list[str]:
    return [f"{m['phase']}/{entry[:-2]}"
            for m in PHASE_LINE.finditer(stdout)
            for entry in m["counts"].split(", ")
            if entry.endswith("=0") and f"{m['phase']}/{entry[:-2]}" not in COVERED_ELSEWHERE]


@needs_reference
@pytest.mark.skipif(not REFERENCE_ALT, reason="FSHAMMER_REFERENCE_ALT_DIR is not set, so cross-mount variants cannot run")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permission checks; run as an ordinary user")
def test_every_variant_passes_on_a_filesystem_that_is_known_good(workdir):
    """Coverage, not just the verdict: a tool that ran nothing exits 0 as happily as one that verified everything."""
    alt = pathlib.Path(REFERENCE_ALT) / workdir.name
    run = hammer(workdir, "--procs", "4", "--seed", "1", "--alt-dir", str(alt))

    assert run.exit == 0, "fshammer failed against the reference filesystem, so the failure is the TOOL's"
    assert "fshammer: OK" in run.stdout, "no clean verdict, so it may not have run at all"
    assert zero_variants(run.stdout) == [], \
        "these variants never completed once on a filesystem where nothing they need is missing"
    assert re.search(r"locks: [1-9]\d* read-modify-writes under an exclusive lock, and the counter holds exactly that", run.stdout), \
        "the locking phase did not complete a single counted read-modify-write"
    assert "DECLARED GAP: --no-lock" not in run.stderr
    assert "namespace/hardlink" not in run.stderr, "hardlinks were declared a gap on a filesystem that has them"
    # the one gap that must ALWAYS be declared: a syscall cannot say how its answer was produced
    assert "fshammer checks results, not speed" in run.stderr, \
        "the vantage-point gap is no longer declared, so a green verdict reads as covering something it cannot see"


@pytest.mark.skipif(not SMALL, reason="FSHAMMER_SMALL_DIR is not set (see scripts/reference-fs.sh)")
def test_running_out_of_space_on_a_small_filesystem_is_honest():
    """Refuse with ENOSPC, or accept and keep: acknowledging a write and storing less is the silent loss.

    The budget is above the filesystem's free space on purpose — that is what makes the tool attempt the
    fill rather than decline it. Four writers, because the fill must not be charged to the variants running
    beside it.
    """
    directory = pathlib.Path(SMALL) / f"run-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        run = hammer(directory, "--procs", "4", "--seed", "2", "--fill-budget", str(2 << 30))
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    malice = next((m for m in PHASE_LINE.finditer(run.stdout) if m["phase"] == "malice"), None)
    assert malice is not None, "no malice phase line"
    assert run.exit == 0, "running out of space is a thing the reference does correctly, so a failure is the TOOL's"
    assert "enospc_is_honest=0" not in malice["counts"], "the ENOSPC check did not run even on a filesystem small enough to fill"
    assert "DECLARED GAP malice/enospc_is_honest" not in run.stderr


@needs_reference
def test_the_oracles_report_a_filesystem_that_lost_something(workdir, tmp_path):
    """One acknowledged file deleted and another truncated, between workload and durability audit: gone AND changed."""
    sabotage = script(tmp_path, "sabotage.sh", own_files(workdir)
                      # two DISTINCT files, or the truncate recreates the deleted one and half the test vanishes
                      + "[ \"$count\" -ge 2 ] || { echo \"need two own-files to damage, found $count\" >&2; exit 1; }\n"
                      + "rm -f \"$(printf '%s\\n' \"$victims\" | sed -n 1p)\"\n"
                      + "printf x > \"$(printf '%s\\n' \"$victims\" | sed -n 2p)\"\n")
    run = hammer(workdir, "--procs", "1", "--seed", "3", "--crash-command", str(sabotage))

    assert run.exit != 0, "a filesystem that lost one acknowledged file and corrupted another was reported as fine"
    assert "fshammer: OK" not in run.stdout
    assert "acknowledged, and gone after the crash" in run.stderr, "the DELETED file was not reported as lost"
    assert "survived the crash holding different bytes" in run.stderr, "the TRUNCATED file was not reported as corrupt"


@needs_reference
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read a mode-000 file")
def test_the_audit_reports_a_file_it_cannot_read_instead_of_dying_on_it(workdir, tmp_path):
    """An audit that RAISES reports nothing — no FAIL line, no verdict — and reads like a hang to whoever runs it.

    Mode 000 rather than delete or truncate: those are answered by comparing bytes, this has to fail the READ.
    Two own-files, so "the audit carried on" has something left to account for.
    """
    revoke = script(tmp_path, "revoke.sh", own_files(workdir)
                    + "[ \"$count\" -ge 2 ] || { echo \"need two own-files so one can be left readable, found $count\" >&2; exit 1; }\n"
                    + "chmod 000 \"$(printf '%s\\n' \"$victims\" | sed -n 1p)\"\n")
    run = hammer(workdir, "--procs", "1", "--seed", "5", "--pre-audit-command", str(revoke))
    checked = re.search(r"own files: (\d+) checked", run.stdout)

    assert "Traceback (most recent call last)" not in run.stderr, "the audit RAISED on a file it could not read"
    assert run.exit != 0, "an acknowledged file could not be read back at audit time, and the run passed"
    assert "fshammer: OK" not in run.stdout
    assert "could not be read back" in run.stderr, "no FAIL line names the unreadable file"
    assert "PermissionError" in run.stderr, "the FAIL line must carry the errno class"
    assert checked and int(checked[1]) > 1, "the audit stopped at the unreadable file instead of carrying on"


def mutate(source: str, anchor: str, replacement: str) -> str:
    """One mutation at EXACTLY one site: no match leaves the real tool running, two damage an unrelated variant."""
    matches = source.count(anchor)
    assert matches == 1, f"the mutation point matched {matches} sites rather than one; this control no longer patches what it claims to"
    return source.replace(anchor, replacement)


@needs_reference
def test_the_contest_oracles_report_a_store_that_let_both_claims_win(workdir, tmp_path):
    """A contest is decided synchronously inside the store, so no damage from OUTSIDE can produce a violation.

    The only way to see these oracles fail is to break the ANSWERS: a mutated copy of the tool with three
    defects in three variants, so none can mask another.
    """
    source = HAMMER.read_text()
    # the loser of a contest is told it won, and another is refused for a reason that is not "the name is taken"
    source = mutate(source,
                    "        try:\n"
                    "            call()\n",
                    "        try:\n"
                    "            if variant == \"file_then_dir\":\n"
                    "                pass\n"
                    "            elif variant == \"dir_then_file\":\n"
                    "                raise PermissionError(13, \"mutant\")\n"
                    "            else:\n"
                    "                call()\n")
    # the delete arrives late and takes the file that replaced the directory
    source = mutate(source,
                    "                    os.rmdir(name)\n"
                    "                    write_whole(name, body)\n"
                    "                    if not os.path.isfile(name):\n",
                    "                    os.rmdir(name)\n"
                    "                    write_whole(name, body)\n"
                    "                    os.unlink(name)\n"
                    "                    if not os.path.isfile(name):\n")
    mutant = tmp_path / "fshammer_mutant.py"
    mutant.write_text(source)

    run = hammer(workdir, "--procs", "2", "--seed", "11", script=mutant)

    assert run.exit != 0, "a store that validated both claims on one name was reported as OK"
    assert "BOTH succeeded on one name" in run.stderr
    assert "rather than EEXIST" in run.stderr
    assert "the delete was applied AFTER the create" in run.stderr


def switches(workdir: pathlib.Path, *args: str) -> Run:
    return hammer(workdir, "--procs", "2", "--seed", "11", *args)


@needs_reference
def test_a_declared_gap_is_reported_and_not_charged_as_a_failure(workdir):
    run = switches(workdir, "--declare-gap", "inode", "--declare-gap", "malice/sparse_far_offset")

    assert run.exit == 0, f"a declared gap was charged as a problem: {run.stderr.strip()[-2000:]}"
    assert "DECLARED GAP inode: the whole phase was declared absent" in run.stderr
    assert "DECLARED GAP malice/sparse_far_offset: --declare-gap" in run.stderr
    assert "inode:" not in run.stdout, "the inode phase was declared absent and still ran its variants"


@needs_reference
def test_a_declaration_that_matched_nothing_is_a_failure(workdir):
    """A misspelled declaration excuses nothing and reads exactly like a correct run, so it is charged."""
    run = switches(workdir, "--declare-gap", "malice/sparse_far_ofset")

    assert run.exit != 0, "a misspelled declaration excused nothing and the run still passed"
    assert "the malice phase has no such variant" in run.stderr
    assert "all sha256 match" in run.stdout, "a failure elsewhere must not make the tool accuse the filesystem of losing bytes"


def lose_one_own_file(tmp_path: pathlib.Path, workdir: pathlib.Path) -> pathlib.Path:
    return script(tmp_path, "lose.sh", own_files(workdir)
                  + "[ \"$count\" -ge 1 ] || { echo 'no own-file to lose' >&2; exit 1; }\n"
                  + "rm -f \"$(printf '%s\\n' \"$victims\" | sed -n 1p)\"\n")


@needs_reference
def test_a_declared_gap_does_not_blunt_the_audit(workdir, tmp_path):
    """A declared gap reduces what is TESTED, never what is AUDITED — or it is a way to make a losing filesystem pass."""
    lose = lose_one_own_file(tmp_path, workdir)
    run = switches(workdir, "--declare-gap", "inode", "--declare-gap", "malice/sparse_far_offset", "--pre-audit-command", str(lose))

    assert run.exit != 0, "a file went missing and the run passed with gaps declared"
    assert "own file could not be read back" in run.stderr
    assert "MISMATCHES ABOVE" in run.stdout


@needs_reference
def test_a_phase_that_is_not_a_variant_rotation_can_be_declared_absent(workdir):
    """`shared` is impossible on a whole-file-PUT store, and its audit must go with it or "0 blocks" reads as clean."""
    run = switches(workdir, "--declare-gap", "shared")

    assert run.exit == 0, f"declaring the shared phase absent was charged as a problem: {run.stderr.strip()[-2000:]}"
    assert "DECLARED GAP shared" in run.stderr
    assert "shared.bin:" not in run.stdout, "the shared.bin verdict still ran"
    assert not (workdir / "shared.bin").exists(), "the phase was declared absent and still wrote the file"


@needs_reference
def test_a_declared_shared_phase_still_fails_on_a_lost_own_file(workdir, tmp_path):
    lose = lose_one_own_file(tmp_path, workdir)
    run = switches(workdir, "--declare-gap", "shared", "--pre-audit-command", str(lose))

    assert run.exit != 0, "a file went missing and the run passed with the shared phase declared"
    assert "own file could not be read back" in run.stderr
    assert "MISMATCHES ABOVE" in run.stdout


def test_an_unknown_phase_name_is_refused_before_the_run_starts(tmp_path):
    run = hammer(tmp_path / "never-created", "--declare-gap", "sharde")

    assert run.exit != 0, "a misspelled phase was accepted"
    assert "no phase called 'sharde'" in run.stderr
    assert "shared" in run.stderr, "the refusal must list the phases"


@needs_reference
def test_a_phase_whose_every_variant_was_refused_is_not_charged_as_not_running(workdir):
    """"Refused" is an answer from the filesystem; "never reached" is a scheduling bug in the tool."""
    run = switches(workdir, "--no-lock")

    assert run.exit == 0, f"--no-lock was charged as a problem: {run.stderr.strip()[-2000:]}"
    assert "DECLARED GAP: --no-lock" in run.stderr
    assert "the locks phase did not run" not in run.stderr


@needs_reference
def test_a_capped_name_length_still_runs_every_length_sensitive_variant(workdir):
    """The cap MOVES the length and drops nothing, says what it gave up, and is probed so a stale cap is noticed."""
    capped = switches(workdir, "--max-name-length", "120")
    shutil.rmtree(workdir)
    workdir.mkdir()
    plain = switches(workdir, "--max-name-length", "255")

    assert capped.exit == 0, f"a capped run was charged as a problem: {capped.stderr.strip()[-2000:]}"
    assert "DECLARED GAP: --max-name-length 120" in capped.stderr
    assert "hostile_names=2" in capped.stdout
    assert "long_names=2" in capped.stdout
    assert "DECLARED GAP CLOSED" in capped.stderr, "the reference holds a 255-byte name, so the probe must call the cap unnecessary"

    assert plain.exit == 0, f"the control run failed: {plain.stderr.strip()[-2000:]}"
    assert "DECLARED GAP: --max-name-length" not in plain.stderr, "NEGATIVE CONTROL: nothing to declare at NAME_MAX"
    assert "DECLARED GAP CLOSED" not in plain.stderr, "nothing probes a cap that was never set"


def test_every_symlink_call_goes_through_the_refusal_wrapper():
    """A raw os.symlink/os.link FAILS on a store with no links, where the wrapper DECLARES the same refusal.

    Structural rather than observed: proving it by running needs a filesystem that refuses symlink().
    """
    raw = []
    for number, line in enumerate(HAMMER.read_text().splitlines(), 1):
        if "os.symlink(" not in line and "os.link(" not in line:
            continue
        if "allowed(os.symlink" in line or "allowed(os.link" in line or line.lstrip().startswith("#"):
            continue
        # the membership test inside entitlement(), not a call
        if "fn in (os.symlink, os.link)" in line:
            continue
        raw.append(f"line {number}: {line.strip()}")
    assert raw == [], "these call the filesystem without allowed():\n" + "\n".join(raw)
