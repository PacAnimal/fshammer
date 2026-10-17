#!/bin/sh
# Samba + Linux SMB3 client: an mtime set with utime() while written data is unflushed is replaced at close.
set -eu
read -r a _ <<EOT
$("$(dirname "$0")/../samba.sh" | tail -n 1)
EOT
python3 - "$a/utime-$$.bin" <<'PY'
import os, sys, time
path = sys.argv[1]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.write(fd, os.urandom(40960))
when = int(time.time()) - 86400
os.utime(path, (when, when))
os.close(fd)
now = int(os.stat(path).st_mtime)
print(f"set mtime {when}, reads {now} after close ({now - when:+d}s)")
if now != when:
    print("REPRODUCED: the mtime set before close did not survive the close")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
