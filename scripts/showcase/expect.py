#!/usr/bin/env python3
"""Run fshammer and require a recorded outcome.

The outcome is the set of FAIL signatures, read from an expectations file with one signature per line:

    FAIL locks/conflict_excludes:        must appear
    ? FAIL shared.bin:                   may appear (seen in some recorded runs, not all)

The job fails if fshammer does not finish, its exit code does not match whether it printed FAIL lines, a
required signature is missing, or a signature appears that the file does not list.

    expect.py --expected FILE --log DIR [--mounted PATH ...] -- --dir /mnt/x/run --duration 0 ...

With --record N it asserts nothing: it runs fshammer N times in fresh directories, prints the signatures, and
writes a proposed expectations file to DIR/expected.txt.
"""
import argparse
import os
import pathlib
import re
import subprocess
import sys

HAMMER = pathlib.Path(__file__).resolve().parents[2] / "fshammer.py"


def run(hammer_args: list[str], timeout: int) -> tuple[int | None, str, str]:
    try:
        proc = subprocess.run([sys.executable, str(HAMMER), *hammer_args],
                              capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return None, out, err


def signature(line: str) -> str:
    """A FAIL line up to its first ': ', comparable across runs: a path becomes its basename, digits become N."""
    subject = line.strip().split(": ", 1)[0][len("FAIL"):].strip()
    if subject.startswith("/"):
        subject = os.path.basename(subject)
    subject = re.sub(r"\d+", "N", subject)
    return f"FAIL {subject}:" if subject else "FAIL:"


def signatures(err: str) -> set[str]:
    return {signature(line) for line in err.splitlines() if line.strip().startswith("FAIL")}


def load(path: pathlib.Path) -> tuple[set[str], set[str]]:
    required, allowed = set(), set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("?"):
            allowed.add(line[1:].strip())
        else:
            required.add(line)
    return required, allowed


def record(args: argparse.Namespace, hammer_args: list[str]) -> int:
    runs = []
    for i in range(1, args.record + 1):
        # fresh directories for each run, and the server's view of them to match
        fresh = [f"{a}-{i}" if prev in ("--dir", "--alt-dir", "--server-root") else a
                 for prev, a in zip([""] + hammer_args, hammer_args)]
        code, out, err = run(fresh, args.timeout)
        (args.log / f"stdout-{i}.txt").write_text(out)
        (args.log / f"stderr-{i}.txt").write_text(err)
        found = signatures(err)
        print(f"run {i}: exit {code}, {len(found)} FAIL signature(s)")
        runs.append(found)
    every = set.intersection(*runs)
    some = set.union(*runs) - every
    proposed = sorted(every) + [f"? {s}" for s in sorted(some)]
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

    required, allowed = load(args.expected)
    code, out, err = run(hammer_args, args.timeout)
    (args.log / "stdout.txt").write_text(out)
    (args.log / "stderr.txt").write_text(err)
    print(out)
    print("FAIL lines reported:")
    for line in err.splitlines():
        if line.lstrip().startswith("FAIL"):
            print(f"  {line.strip()[:300]}")

    found = signatures(err)
    want = 1 if found else 0
    problems = []
    if code is None:
        problems.append(f"fshammer did not finish within {args.timeout}s")
    elif code != want:
        problems.append(f"fshammer exited {code}, expected {want}")
    problems += [f"expected and missing: {s}" for s in sorted(required - found)]
    problems += [f"not expected: {s}" for s in sorted(found - required - allowed)]

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
