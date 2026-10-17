#!/usr/bin/env python3
"""Run fshammer and require a known outcome: an exit code, and lines that must appear on stderr.

A showcase records how a filesystem behaves today. If that changes — a failure stops happening, or the run
starts passing — this exits non-zero so the change is noticed and the showcase updated.

    expect.py --exit 1 --stderr 'FAIL ...' [--stderr ...] --mounted /mnt/x --log DIR -- --dir /mnt/x/run --duration 0

With --record N it asserts nothing: it runs fshammer N times, each in fresh directories, and prints the FAIL
signatures that appeared in every run, for confirming a target's outcome before recording it.
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


def signatures(err: str) -> set[str]:
    """Each FAIL line up to its first colon, with the run directory replaced, so runs can be compared."""
    found = set()
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("FAIL"):
            found.add(re.sub(r"/run-[^/]*", "/RUN", line.split(": ", 1)[0]) + ":")
    return found


def record(args: argparse.Namespace, hammer_args: list[str]) -> int:
    common = None
    for i in range(1, args.record + 1):
        # fresh directories for each run, and the server's view of them to match
        fresh = [f"{a}-{i}" if prev in ("--dir", "--alt-dir", "--server-root") else a
                 for prev, a in zip([""] + hammer_args, hammer_args)]
        code, out, err = run(fresh, args.timeout)
        (args.log / f"stdout-{i}.txt").write_text(out)
        (args.log / f"stderr-{i}.txt").write_text(err)
        found = signatures(err)
        print(f"run {i}: exit {code}, {len(found)} FAIL signature(s)")
        common = found if common is None else common & found
    print("FAIL signatures in every run:")
    for line in sorted(common or ()):
        print(f"  {line}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exit", type=int, required=True, help="the exit code fshammer must return")
    ap.add_argument("--stderr", action="append", default=[], help="a substring that must appear on stderr; repeatable")
    ap.add_argument("--log", type=pathlib.Path, required=True, help="where to write stdout.txt and stderr.txt")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before the run is killed (default 1800)")
    ap.add_argument("--mounted", action="append", default=[], help="a path that must be a mount point; repeatable")
    ap.add_argument("--record", type=int, default=0, metavar="N",
                    help="run N times and print the FAIL signatures common to all runs, asserting nothing")
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
    code, out, err = run(hammer_args, args.timeout)
    (args.log / "stdout.txt").write_text(out)
    (args.log / "stderr.txt").write_text(err)

    print(out)
    print("FAIL lines reported:")
    for line in err.splitlines():
        if line.lstrip().startswith("FAIL"):
            print(f"  {line.strip()[:300]}")

    problems = []
    if code is None:
        problems.append(f"fshammer did not finish within {args.timeout}s")
    elif code != args.exit:
        problems.append(f"fshammer exited {code}, expected {args.exit}")
    problems += [f"not on stderr: {wanted!r}" for wanted in args.stderr if wanted not in err]

    print()
    if problems:
        print("OUTCOME CHANGED — update this showcase:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"outcome as expected: exit {code}, {len(args.stderr)} expected line(s) present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
