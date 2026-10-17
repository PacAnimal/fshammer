#!/bin/sh
# sshfs, built from its latest release against the latest libfuse, to the local sshd, mounted twice. Extra
# arguments are passed to sshfs as -o options. Prints the two mount points.
set -eu

base="${SHOWCASE_BASE:-/tmp/showcase}"
extra="${1:+,$1}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server >/dev/null
"$(dirname "$0")/build.sh" sshfs

# a key the invoking user can log in to itself with
mkdir -p ~/.ssh "$base/sshfs-export" "$base/sshfs-a" "$base/sshfs-b"
[ -f ~/.ssh/showcase ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/showcase
cat ~/.ssh/showcase.pub >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
sudo mkdir -p /run/sshd
sudo service ssh start >/dev/null 2>&1 || sudo /usr/sbin/sshd

for mount in "$base/sshfs-a" "$base/sshfs-b"; do
    /usr/local/bin/sshfs "$(id -un)@127.0.0.1:$base/sshfs-export" "$mount" \
        -o "IdentityFile=$HOME/.ssh/showcase,StrictHostKeyChecking=no,UserKnownHostsFile=/dev/null$extra" >&2
done
echo "$base/sshfs-a $base/sshfs-b"
