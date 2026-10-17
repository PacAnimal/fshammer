#!/usr/bin/env python3
"""Writes a shields.io endpoint badge per showcase target, and summary.json, from the jobs' result.json files.

    status.py ARTIFACTS_DIR OUT_DIR RUN_URL

ARTIFACTS_DIR holds a showcase-<target> directory per fshammer job and a repro-<id> directory per reproducer
job. A target with no result.json did not get as far as running fshammer, so its setup failed. A reproducer
whose outcome differs from repros.txt marks its target "reproducer changed".
"""
import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
EXPECTED = HERE / "expected"
sys.path.insert(0, str(HERE))
import repros  # noqa: E402


def badge(result: dict | None, repro_results: list[dict | None]) -> tuple[str, str, str]:
    """The badge message and colour, and the outcome word for summary.json, describing what this run did."""
    if result is None:
        return "setup failed", "red", "setup failed"
    if not result["matched"]:
        return "changed", "red", "changed"
    if any(r is None or not r["matched"] for r in repro_results):
        return "reproducer changed", "red", "reproducer changed"
    if not result["finished"]:
        return "did not finish", "red", "did not finish"
    if result["fails"] == 0 and result["exit"] == 0:
        return "no failures", "brightgreen", "as recorded"
    checks = "failing check" if result["fails"] == 1 else "failing checks"
    if set(result.get("classes", [])) == {"limitation"}:
        return f"{result['fails']} {checks} (documented)", "yellow", "as recorded"
    return f"{result['fails']} {checks}", "orange", "as recorded"


def main() -> int:
    artifacts, out, run_url = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = []
    for expected in sorted(EXPECTED.glob("*.txt")):
        target = expected.stem
        path = artifacts / f"showcase-{target}" / "result.json"
        result = json.loads(path.read_text()) if path.exists() else None
        repro_results = []
        for entry in (e for e in repros.entries() if e["target"] == target):
            rpath = artifacts / f"repro-{entry['id']}" / "result.json"
            repro_results.append(json.loads(rpath.read_text()) if rpath.exists() else None)
        message, color, outcome = badge(result, repro_results)
        (out / f"{target}.json").write_text(json.dumps(
            {"schemaVersion": 1, "label": target, "message": message, "color": color}))
        summary.append({"target": target, "exit": result and result["exit"], "fails": result and result["fails"],
                        "classes": result and result.get("classes"),
                        "outcome": outcome, "run": run_url, "kernel": result and result["kernel"],
                        "reproducers": {r["id"]: r["outcome"] for r in repro_results if r},
                        "timestamp": now})
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    for row in summary:
        print(f"{row['target']}: {row['outcome']}, exit {row['exit']}, {row['fails']} distinct failing check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
