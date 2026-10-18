#!/usr/bin/env python3
"""Writes the showcase tables in README.md and showcase/README.md from targets.txt, expected/ and repros.txt.

    table.py           rewrite both tables
    table.py --check   exit 1 if either file differs from what would be written

The tables sit between <!-- showcase-table --> and <!-- /showcase-table --> markers.
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import expect  # noqa: E402
import repros  # noqa: E402

PAGE = "https://github.com/PacAnimal/fshammer/blob/main/showcase/README.md"
WORKFLOW = "https://github.com/PacAnimal/fshammer/actions/workflows/showcase.yml"
BADGE = "https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PacAnimal/fshammer/status/{}.json"
ROOT_LABELS = {"fixed": "fixed upstream", "open": "not established", "setup": "probably the setup"}
CLASS_NAMES = (("defect", "likely defect"), ("follows", "follows from another failure"),
               ("open", "not established"), ("limitation", "documented"))


def targets() -> list[dict]:
    rows = []
    for line in (HERE / "targets.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        tid, name, version, section, root, defect_in = (part.strip() for part in line.split("|"))
        rows.append({"id": tid, "name": name, "version": version, "section": section, "root": root,
                     "defect_in": defect_in})
    return rows


def checks(tid: str) -> tuple[set[str], set[str], dict[str, str]]:
    required, allowed, tags = expect.load(HERE / "expected" / f"{tid}.txt")
    return {s for s in required if s != "TIMEOUT"}, {s for s in allowed if s != "TIMEOUT"}, tags


def finishing(tid: str) -> str:
    """Whether the recorded runs finished: "" for always, else how it is recorded."""
    required, allowed, _ = expect.load(HERE / "expected" / f"{tid}.txt")
    if "TIMEOUT" in required:
        return "does not finish"
    if "TIMEOUT" in allowed:
        return "may not finish"
    return ""


def what_fails(t: dict) -> str:
    """The root failure when there is one, then failing checks per class, e.g. "likely defect: 2 checks (`a`, `b`)"."""
    tid = t["id"]
    required, allowed, tags = checks(tid)
    parts = []
    finish = finishing(tid)
    if t["root"]:
        kind, _, what = t["root"].partition(": ")
        parts.append(f"{ROOT_LABELS[kind]}: {what}")
    elif finish == "does not finish":
        parts.append("not established: fshammer does not finish")
    for tag, label in CLASS_NAMES:
        always = sorted(s for s in required if tags[s] == tag)
        sometimes = sorted(s for s in allowed if tags[s] == tag)
        if not always and not sometimes:
            continue
        n = len(always) + len(sometimes)
        noun = "check" if n == 1 else "checks"
        if not always:
            text = f"{label}: {n} {noun}, in some runs"
        else:
            text = f"{label}: {len(always)} {'check' if len(always) == 1 else 'checks'}"
            text += f" (+{len(sometimes)} in some runs)" if sometimes else ""
        if tag == "follows" and t["root"]:
            text = text.replace(label, "following from it", 1)
        if tag == "defect":
            text = text.replace(label, f"{label} ({t['defect_in']})", 1)
        # named when few enough to read; the section of showcase/README.md has the rest
        if tag == "defect" and n <= 4:
            text += " (" + ", ".join(f"`{s[len('FAIL '):-1]}`" for s in always + sometimes) + ")"
        parts.append(text)
    if finish == "may not finish":
        parts.append("may not finish")
    if not parts:
        return "None in the recorded runs."
    return "; ".join(parts) + "."


def readme_table() -> str:
    lines = ["| Target | Status | Failing checks |", "|---|---|---|"]
    for t in targets():
        name = f"[{t['name']}]({PAGE}#{t['section']})" if t["section"] else t["name"]
        badge = f"[![{t['id']}]({BADGE.format(t['id'])})]({WORKFLOW})"
        lines.append(f"| {name} | {badge} | {what_fails(t)} |")
    return "\n".join(lines)


def showcase_table() -> str:
    reproduced, controls = {}, {}
    for entry in repros.entries():
        kind = reproduced if entry["expect"] in ("reproduced", "sometimes") else controls
        for target in [entry["target"], *entry["also"]]:
            kind[target] = kind.get(target, 0) + 1
    lines = ["| Target | Version | Distinct failing checks | Reproducers | Controls |", "|---|---|---|---|---|"]
    for t in targets():
        required, allowed, _ = checks(t["id"])
        cell = f"{len(required)}" + (f", {len(allowed)} optional" if allowed else "")
        if finishing(t["id"]):
            cell += f"; {finishing(t['id'])}"
        name = f"[{t['name']}](#{t['section']})" if t["section"] else t["name"]
        lines.append(f"| {name} | {t['version']} | {cell} | {reproduced.get(t['id'], '')} | {controls.get(t['id'], '')} |")
    return "\n".join(lines)


def checks_raw(tid: str) -> set[str]:
    required, allowed, _ = expect.load(HERE / "expected" / f"{tid}.txt")
    return required | allowed


def replace(path: pathlib.Path, table: str) -> tuple[str, str]:
    text = path.read_text()
    start, end = "<!-- showcase-table -->", "<!-- /showcase-table -->"
    i, j = text.index(start) + len(start), text.index(end)
    return text, text[:i] + "\n" + table + "\n" + text[j:]


def anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def main() -> int:
    check = "--check" in sys.argv[1:]
    page = (ROOT / "showcase" / "README.md").read_text()
    headings = {anchor(h) for h in re.findall(r"^#+ (.+)$", page, re.M)}
    missing = sorted({t["section"] for t in targets() if t["section"] and t["section"] not in headings})
    if missing:
        print(f"targets.txt names sections showcase/README.md does not have: {', '.join(missing)}")
        return 1
    # a likely defect names its component
    unnamed = [t["id"] for t in targets() if not t["defect_in"] and "defect" in checks(t["id"])[2].values()]
    if unnamed:
        print(f"targets.txt names no defect component for: {', '.join(unnamed)}")
        return 1
    # a likely defect is only called one with a reproducer behind it: every [defect] check must be covered
    covered = {(target, f"FAIL {check}:") for e in repros.entries() if e["expect"] == "reproduced"
               for target in [e["target"], *e["also"]] for check in e["covers"]}
    unbacked = sorted(f"{t['id']}: {sig}" for t in targets() for sig, tag in checks(t["id"])[2].items()
                      if tag == "defect" and (t["id"], sig) not in covered)
    if unbacked:
        print("[defect] checks no reproducer in repros.txt covers:\n  " + "\n  ".join(unbacked))
        return 1
    listed = {t["id"] for t in targets()}
    files = {p.stem for p in (HERE / "expected").glob("*.txt")}
    if listed != files:
        print(f"targets.txt and expected/ disagree: {sorted(listed ^ files)}")
        return 1
    stale = []
    for path, table in ((ROOT / "README.md", readme_table()), (ROOT / "showcase" / "README.md", showcase_table())):
        old, new = replace(path, table)
        if old != new:
            stale.append(path.relative_to(ROOT))
            if not check:
                path.write_text(new)
    if check and stale:
        print(f"out of date, run scripts/showcase/table.py: {', '.join(map(str, stale))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
