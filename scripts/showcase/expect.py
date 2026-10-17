#!/usr/bin/env python3
"""Run fshammer and require a recorded outcome.

The outcome is the set of FAIL signatures, read from an expectations file with one signature per line, each
tagged with how showcase/README.md classifies it: [defect] (a likely defect), [follows] (fails because of
another failure of the same target), [open] (not established) or [limitation] (documented, with a source):

    FAIL locks/conflict_excludes: [defect]       must appear
    ? FAIL shared.bin: [limitation]              may appear (seen in some recorded runs, not all)
    ? TIMEOUT [open]                             fshammer may not finish; the other lines are then not required
    TIMEOUT [open]                               fshammer must not finish within --timeout

The job fails if fshammer does not finish, its exit code does not match whether it printed FAIL lines, a
required signature is missing, or a signature appears that the file does not list.

    expect.py --expected FILE --log DIR [--mounted PATH ...] -- --dir /mnt/x/run --duration 0 ...

With --record N it asserts nothing: it runs fshammer N times in fresh directories, prints the signatures, and
writes a proposed expectations file to DIR/expected.txt.
"""
import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys

HAMMER = pathlib.Path(__file__).resolve().parents[2] / "fshammer.py"


def snapshot(sid: int) -> str:
    """Each process of a run that did not finish: its state, what it waits on in the kernel, and its stack."""
    ps = subprocess.run(["ps", "-e", "-o", "pid=,sid=,stat=,wchan:32=,args="], capture_output=True, text=True)
    mine = [line for line in ps.stdout.splitlines() if line.split()[1:2] == [str(sid)]]
    lines = ["PID SID STAT WCHAN COMMAND", *mine]
    for pid in (line.split()[0] for line in mine):
        try:
            stack = subprocess.run(["sudo", "-n", "cat", f"/proc/{pid}/stack"], capture_output=True, text=True)
            lines.append(f"--- /proc/{pid}/stack\n{stack.stdout or stack.stderr}")
        except OSError as e:
            lines.append(f"--- /proc/{pid}/stack unavailable: {e}")
    return "\n".join(lines)


def run(hammer_args: list[str], timeout: int, log: pathlib.Path, suffix: str = "") -> tuple[int | None, str, str]:
    """Output goes to files: a process stuck in the kernel can keep a pipe open after it is killed."""
    out_path, err_path = log / f"stdout{suffix}.txt", log / f"stderr{suffix}.txt"
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen([sys.executable, str(HAMMER), *hammer_args], stdout=out, stderr=err,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
            state = snapshot(proc.pid)
            (log / f"hang{suffix}.txt").write_text(state)
            print(f"fshammer did not finish within {timeout}s; process states:\n{state}")
            os.killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("fshammer did not exit after SIGKILL")
    return code, out_path.read_text(errors="replace"), err_path.read_text(errors="replace")


def signature(line: str) -> str:
    """A FAIL line up to its first ': ', comparable across runs: a path becomes its basename, digits become N."""
    subject = line.strip().split(": ", 1)[0][len("FAIL"):].strip()
    if subject.startswith("/"):
        subject = os.path.basename(subject)
    subject = re.sub(r"\d+", "N", subject)
    return f"FAIL {subject}:" if subject else "FAIL:"


def signatures(err: str) -> set[str]:
    return {signature(line) for line in err.splitlines() if line.strip().startswith("FAIL")}


TAGS = ("defect", "follows", "open", "limitation")


def load(path: pathlib.Path) -> tuple[set[str], set[str], dict[str, str]]:
    """The required and allowed signatures, and each one's tag."""
    required, allowed, tags = set(), set(), {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(\?\s*)?(.+?)\s+\[(\w+)\]", line)
        if not match or match[3] not in TAGS:
            raise SystemExit(f"{path}: every signature needs a tag, one of {', '.join(TAGS)}: {line!r}")
        optional, sig, tags[match[2]] = match[1], match[2], match[3]
        (allowed if optional else required).add(sig)
    return required, allowed, tags


def record(args: argparse.Namespace, hammer_args: list[str]) -> int:
    runs = []
    for i in range(1, args.record + 1):
        # fresh directories for each run, and the server's view of them to match
        fresh = [f"{a}-{i}" if prev in ("--dir", "--alt-dir", "--server-root") else a
                 for prev, a in zip([""] + hammer_args, hammer_args)]
        code, out, err = run(fresh, args.timeout, args.log, f"-{i}")
        found = signatures(err) | ({"TIMEOUT"} if code is None else set())
        print(f"run {i}: exit {code}, {len(found)} FAIL signature(s)")
        runs.append(found)
    every = set.intersection(*runs)
    some = set.union(*runs) - every
    # keep the classification of signatures already listed; a new one needs one by hand
    known = load(args.expected)[2] if args.expected.exists() else {}
    proposed = ([f"{s} [{known.get(s, 'UNCLASSIFIED')}]" for s in sorted(every)]
                + [f"? {s} [{known.get(s, 'UNCLASSIFIED')}]" for s in sorted(some)])
    (args.log / "expected.txt").write_text("".join(f"{line}\n" for line in proposed))
    print("proposed expectations:")
    for line in proposed:
        print(f"  {line}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expected", type=pathlib.Path, required=True, help="the expectations file")
    ap.add_argument("--log", type=pathlib.Path, required=True, help="where to write fshammer's stdout and stderr")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before the run is killed (default 1800)")
    ap.add_argument("--mounted", action="append", default=[], help="a path that must be a mount point; repeatable")
    ap.add_argument("--record", type=int, default=0, metavar="N",
                    help="run N times and propose expectations, asserting nothing")
    ap.add_argument("args", nargs=argparse.REMAINDER, help="arguments for fshammer, after --")
    args = ap.parse_args()
    hammer_args = args.args[1:] if args.args[:1] == ["--"] else args.args

    # a mount that silently failed leaves an ordinary directory, and the run would test the wrong filesystem
    unmounted = [path for path in args.mounted if not os.path.ismount(path)]
    if unmounted:
        print(f"not mounted: {', '.join(unmounted)}")
        return 1

    args.log.mkdir(parents=True, exist_ok=True)
    if args.record:
        return record(args, hammer_args)

    required, allowed, tags = load(args.expected)
    code, out, err = run(hammer_args, args.timeout, args.log)
    print(out)
    print("FAIL lines reported:")
    for line in err.splitlines():
        if line.lstrip().startswith("FAIL"):
            print(f"  {line.strip()[:300]}")

    found = signatures(err)
    want = 1 if found else 0
    problems = []
    if code is None:
        if "TIMEOUT" not in allowed | required:
            problems.append(f"fshammer did not finish within {args.timeout}s")
    elif "TIMEOUT" in required:
        problems.append(f"fshammer finished (exit {code}), expected it not to within {args.timeout}s")
    elif code != want:
        problems.append(f"fshammer exited {code}, expected {want}")
    if code is not None:
        problems += [f"expected and missing: {s}" for s in sorted(required - found)]
    problems += [f"not expected: {s}" for s in sorted(found - required - allowed)]

    # read by the workflow's status job
    (args.log / "result.json").write_text(json.dumps({
        "exit": code, "fails": len(found), "matched": not problems, "finished": code is not None,
        "kernel": os.uname().release, "classes": sorted({tags.get(s, "unlisted") for s in found})}))

    print()
    if problems:
        print(f"OUTCOME CHANGED — update {args.expected}:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"outcome as expected: exit {code}, {len(found)} FAIL signature(s), all listed in {args.expected.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
