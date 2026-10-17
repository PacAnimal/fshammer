#!/bin/sh
# davfs2 mounting a local apache2 mod_dav share. Prints the mount point.
# Usage: davfs2.sh [upstream|ubuntu]: davfs2 built from its latest release (default), or Ubuntu's package.
set -eu

variant="${1:-upstream}"
base="${SHOWCASE_BASE:-/tmp/showcase}"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apache2 >/dev/null
case "$variant" in
    upstream) "$(dirname "$0")/build.sh" davfs2; mount_davfs=/usr/local/sbin/mount.davfs ;;
    ubuntu)   sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq davfs2 >/dev/null; mount_davfs=/sbin/mount.davfs ;;
    *)        echo "unknown variant: $variant" >&2; exit 2 ;;
esac

# the share
sudo mkdir -p /var/www/dav /var/lib/apache2/davlock
sudo chown www-data:www-data /var/www/dav /var/lib/apache2/davlock
sudo tee /etc/apache2/conf-available/showcase-dav.conf >/dev/null <<'CONF'
DavLockDB /var/lib/apache2/davlock/lockdb
Alias /dav /var/www/dav
<Directory /var/www/dav>
    Dav On
    Require all granted
</Directory>
CONF
sudo a2enmod -q dav dav_fs >/dev/null
sudo a2enconf -q showcase-dav >/dev/null
sudo apache2ctl -k restart >&2

# the mount, owned by the invoking user, with no credentials to ask for
mkdir -p "$base/dav"
sudo mkdir -p /etc/davfs2
echo "$base/dav \"\" \"\"" | sudo tee -a /etc/davfs2/secrets >/dev/null
sudo chmod 600 /etc/davfs2/secrets
sudo "$mount_davfs" http://127.0.0.1/dav "$base/dav" -o "uid=$(id -u),gid=$(id -g)" >&2
echo "$base/dav"
