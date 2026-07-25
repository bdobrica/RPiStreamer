#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "rpi-streamer backup: run as root (normally through sudo)" >&2
    exit 2
fi

destination=${1:-/var/backups/rpi-streamer}
case "$destination" in
    /*) ;;
    *)
        echo "rpi-streamer backup: destination must be absolute" >&2
        exit 2
        ;;
esac
install -d -m 0750 "$destination"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive=$destination/rpi-streamer-$stamp.tgz
was_active=false
if systemctl is-active --quiet rpi-streamer; then
    was_active=true
    systemctl stop rpi-streamer
fi
trap 'if [ "$was_active" = true ]; then systemctl start rpi-streamer; fi' EXIT

tar -czf "$archive" \
    /etc/rpi-streamer \
    /etc/systemd/system/rpi-streamer.service \
    /etc/nginx/sites-available/rpi-streamer.conf \
    /var/lib/rpi-streamer
chmod 0640 "$archive"
echo "$archive"
