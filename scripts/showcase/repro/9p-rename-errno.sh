#!/bin/sh
# Linux 9p client with u9fs: renaming a directory that holds an open file fails with errno 526, which is
# ESERVERFAULT, a kernel-internal value that should not reach user programs.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" u9fs none 1 | tail -n 1 | cut -d' ' -f1)"
python3 - "$dir" <<'PY'
import os, sys
leaked = []
for parent in (sys.argv[1], os.path.join(sys.argv[1], f"outer-{os.getpid()}")):
    top = os.path.join(parent, f"tall-{os.getpid()}")
    leaf_dir = os.path.join(top, *[f"lvl{i}" for i in range(12)])
    os.makedirs(leaf_dir)
    fd = os.open(os.path.join(leaf_dir, "held.bin"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.write(fd, b"a" * 2048)
    try:
        os.rename(top, top + "-moved")
        print(f"{top}: rename succeeded")
    except OSError as e:
        print(f"{top}: rename failed with errno {e.errno} ({e.strerror})")
        if e.errno >= 512:
            leaked.append(e.errno)
    os.close(fd)
if leaked:
    print(f"REPRODUCED: rename returned kernel-internal errno {leaked[0]}")
    sys.exit(0)
print("not reproduced")
sys.exit(1)
PY
