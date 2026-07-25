#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "rpi-streamer uninstaller: run as root (normally through sudo)" >&2
    exit 2
fi

systemctl disable --now rpi-streamer 2>/dev/null || true
rm -f /etc/systemd/system/rpi-streamer.service
rm -f /etc/nginx/sites-enabled/rpi-streamer.conf
rm -f /etc/nginx/sites-available/rpi-streamer.conf
systemctl daemon-reload
nginx -t && systemctl reload nginx
echo "Application units removed; configuration, state, media, and account retained."
