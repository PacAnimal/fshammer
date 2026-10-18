#!/usr/bin/env python3
"""The standalone reproducers listed in repros.txt.

    repros.py matrix [ONLY]     the workflow matrix as JSON; ONLY is a space-separated list of ids or targets
    repros.py run ID LOG_DIR    run one, write LOG_DIR/output.txt and LOG_DIR/result.json, and exit non-zero
                                if the outcome is not the expected one

A reproducer script prints REPRODUCED and exits 0, or prints "not reproduced" and exits 1; anything else is an
error.
"""
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def entries() -> list[dict]:
    rows = []
    for line in (HERE / "repros.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        rid, target, expect, *rest = line.split()
        assert expect in ("reproduced", "not-reproduced"), f"{rid}: unknown outcome {expect!r}"
        # optional covers=<check>,<check> (the failing checks it backs) and also=<target>,<target>
        options = {}
        while rest and "=" in rest[0]:
            key, _, value = rest.pop(0).partition("=")
            options[key] = value.split(",")
        script, *args = rest
        rows.append({"id": rid, "target": target, "expect": expect, "script": script, "args": args,
                     "covers": options.get("covers", []), "also": options.get("also", [])})
    return rows


def main() -> int:
    command = sys.argv[1]
    if command == "matrix":
        only = set(sys.argv[2].split()) if len(sys.argv) > 2 and sys.argv[2].strip() else None
        chosen = [e["id"] for e in entries() if only is None or e["id"] in only or e["target"] in only]
        print(json.dumps(chosen))
        return 0

    rid, log = sys.argv[2], pathlib.Path(sys.argv[3])
    entry = next(e for e in entries() if e["id"] == rid)
    log.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(HERE / "repro" / entry["script"]), *entry["args"]],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
    (log / "output.txt").write_text(proc.stdout)
    print(proc.stdout)
    # the exit code and the script's own verdict line must agree, so a setup step failing with 1 is an error
    if proc.returncode == 0 and "REPRODUCED" in proc.stdout:
        outcome = "reproduced"
    elif proc.returncode == 1 and "not reproduced" in proc.stdout:
        outcome = "not-reproduced"
    else:
        outcome = "error"
    result = {**entry, "outcome": outcome, "matched": outcome == entry["expect"]}
    (log / "result.json").write_text(json.dumps(result))
    print(f"{rid}: {outcome}, expected {entry['expect']}")
    return 0 if result["matched"] else 1


if __name__ == "__main__":
    sys.exit(main())
