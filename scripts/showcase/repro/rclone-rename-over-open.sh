#!/bin/sh
# rclone mount --vfs-cache-mode writes: a file is renamed over another that is held open with O_APPEND, and one
# byte is written through the held descriptor; the name then does not hold the renamed file's content.
set -eu
dir="$("$(dirname "$0")/../rclone.sh" writes | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir/over-$$" <<'PY'
import os, sys
base = sys.argv[1]
src, dst = base + "-src.bin", base + "-dst.bin"
with open(dst, "wb") as fh:
    fh.write(b"V" * 4096)
with open(src, "wb") as fh:
    fh.write(b"S" * 4096)
held = os.open(dst, os.O_WRONLY | os.O_APPEND)
os.rename(src, dst)
try:
    os.write(held, b"G")
except OSError as e:
    print("write through the held descriptor failed:", e)
os.close(held)
try:
    with open(dst, "rb") as fh:
        got = fh.read()
except OSError as e:
    print(f"REPRODUCED: the name cannot be opened after the rename: {e}")
    sys.exit(0)
print(f"the name holds {len(got)} bytes ({got[:1]!r}...); the renamed file had 4096 bytes of b'S'")
if got != b"S" * 4096:
    print("REPRODUCED: the name does not hold the renamed file's content")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
