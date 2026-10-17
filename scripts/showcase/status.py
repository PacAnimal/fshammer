#!/usr/bin/env python3
"""Writes a shields.io endpoint badge per showcase target, and summary.json, from the jobs' result.json files.

    status.py ARTIFACTS_DIR OUT_DIR RUN_URL

ARTIFACTS_DIR holds one showcase-<target> directory per job. A target with no result.json did not get as far
as running fshammer, so its setup failed.
"""
import datetime
import json
import pathlib
import sys

EXPECTED = pathlib.Path(__file__).resolve().parent / "expected"


def badge(result: dict | None) -> tuple[str, str, str]:
    """The badge message and colour, and the outcome word for summary.json."""
    if result is None:
        return "setup failed", "red", "setup failed"
    if not result["matched"]:
        return "changed", "red", "changed"
    if not result["finished"]:
        return "did not finish", "orange", "as recorded"
    if result["fails"] == 0:
        return "passes", "brightgreen", "as recorded"
    return f"{result['fails']} failures", "orange", "as recorded"


def main() -> int:
    artifacts, out, run_url = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = []
    for expected in sorted(EXPECTED.glob("*.txt")):
        target = expected.stem
        path = artifacts / f"showcase-{target}" / "result.json"
        result = json.loads(path.read_text()) if path.exists() else None
        message, color, outcome = badge(result)
        (out / f"{target}.json").write_text(json.dumps(
            {"schemaVersion": 1, "label": target, "message": message, "color": color}))
        summary.append({"target": target, "exit": result and result["exit"], "fails": result and result["fails"],
                        "outcome": outcome, "run": run_url, "kernel": result and result["kernel"],
                        "timestamp": now})
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    for row in summary:
        print(f"{row['target']}: {row['outcome']}, exit {row['exit']}, {row['fails']} FAIL signature(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
