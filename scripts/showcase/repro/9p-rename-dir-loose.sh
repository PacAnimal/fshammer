#!/bin/sh
# diod with cache=loose: a file read before its directory is renamed is listed under the new name but opening
# it fails with ENOENT.
set -eu
dir="$("$(dirname "$0")/../v9fs.sh" diod loose 1 | tail -n 1 | cut -d' ' -f1)"
cd "$dir"
mkdir "d-$$"
echo hello > "d-$$/f"
cat "d-$$/f" >/dev/null
mv "d-$$" "d2-$$"
echo "listing: $(ls "d2-$$")"
if cat "d2-$$/f"; then
    echo "not reproduced"
    exit 1
fi
echo "REPRODUCED: d2-$$/f is listed but cannot be opened"
