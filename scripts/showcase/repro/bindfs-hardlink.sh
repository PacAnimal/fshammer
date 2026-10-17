#!/bin/sh
# bindfs: after link(a, b), a write through a is not visible when b is read.
set -eu
dir="$("$(dirname "$0")/../bindfs.sh" | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/link-$$" <<'PY'
import os, sys
base = sys.argv[1]
first, second = base + "-a.bin", base + "-b.bin"
with open(first, "wb") as fh:
    fh.write(b"one")
os.link(first, second)
with open(second, "rb") as fh:
    fh.read()
with open(first, "r+b") as fh:
    fh.write(b"two")
with open(second, "rb") as fh:
    got = fh.read()
print(f"wrote b'two' through the first name, read {got!r} through the second")
if got != b"two":
    print("REPRODUCED: the second name returned stale content")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
