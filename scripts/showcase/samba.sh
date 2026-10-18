#!/bin/sh
# Samba (smbd) sharing a local ext4 directory, mounted twice over loopback with the kernel SMB3 client; the
# second mount uses nosharesock so it is a separate session. Without the SMB3 POSIX extensions, which
# Samba 4.19 as packaged in Ubuntu 24.04 does not serve. Prints the two mount points and the shared directory.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq samba cifs-utils >/dev/null

share="$base/samba-share"
a="$base/samba-a"
b="$base/samba-b"
mkdir -p "$a" "$b"
"$(dirname "$0")/ext4.sh" "$share"

sudo tee /etc/samba/smb.conf >/dev/null <<CONF
[global]
    server min protocol = SMB3
    map to guest = never
[share]
    path = $share
    read only = no
    valid users = $(id -un)
CONF
printf 'showcase\nshowcase\n' | sudo smbpasswd -s -a "$(id -un)" >/dev/null
sudo mkdir -p /run/samba
testparm -s >/dev/null 2>&1 || { testparm -s >&2; exit 1; }
# the package may already have started smbd with its default configuration
sudo systemctl restart smbd 2>/dev/null || { sudo pkill -x smbd || true; sudo smbd -D; }
for _ in $(seq 50); do ss -ltn | grep -q ':445 ' && break; sleep 0.2; done

opts="vers=3.1.1,username=$(id -un),password=showcase,uid=$(id -u),gid=$(id -g)"
sudo mount -t cifs //127.0.0.1/share "$a" -o "$opts" >&2
sudo mount -t cifs //127.0.0.1/share "$b" -o "$opts,nosharesock" >&2
echo "$a $b $share"
