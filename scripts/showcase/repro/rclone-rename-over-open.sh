#!/bin/sh
# rclone mount --vfs-cache-mode writes: renaming a file over another that is still open for writing.
set -eu
dir="$("$(dirname "$0")/../rclone.sh" writes | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/over-$$" <<'PY'
import os, sys
base = sys.argv[1]
a, b = base + "-a.bin", base + "-b.bin"
with open(a, "wb") as fh:
    fh.write(b"A" * 4096)
held = os.open(b, os.O_WRONLY | os.O_CREAT, 0o644)
os.write(held, b"B" * 100)
os.rename(a, b)
os.write(held, b"B")
os.close(held)
try:
    with open(b, "rb") as fh:
        got = fh.read()
except OSError as e:
    print(f"REPRODUCED: the renamed file cannot be read under its new name: {e}")
    sys.exit(0)
print(f"{b} holds {len(got)} bytes; the renamed file had 4096")
if got != b"A" * 4096:
    print("REPRODUCED: the name does not hold the renamed file's content")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
