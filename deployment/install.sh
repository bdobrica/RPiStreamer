#!/bin/sh
set -eu

usage() {
    echo "usage: sudo deployment/install.sh WHEEL [LISTEN_ADDRESS]" >&2
    echo "example: sudo deployment/install.sh dist/rpi_streamer-*.whl 192.168.1.20:8080" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    usage
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "rpi-streamer installer: run as root (normally through sudo)" >&2
    exit 2
fi

wheel=$1
listen=${2:-127.0.0.1:8080}
case "$wheel" in
    /*) ;;
    *) wheel="$(pwd)/$wheel" ;;
esac
if [ ! -f "$wheel" ]; then
    echo "rpi-streamer installer: wheel not found: $wheel" >&2
    exit 2
fi
case "$listen" in
    *[\&\|\;\`\'\"\\[:space:]]*)
        echo "rpi-streamer installer: invalid listen address: $listen" >&2
        exit 2
        ;;
esac

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for command in python3 systemd-sysusers systemd-tmpfiles nginx; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "rpi-streamer installer: required command not found: $command" >&2
        exit 3
    fi
done

install -d -m 0755 /opt/rpi-streamer
if [ ! -x /opt/rpi-streamer/venv/bin/python ]; then
    python3 -m venv /opt/rpi-streamer/venv
fi
/opt/rpi-streamer/venv/bin/python -m pip install --upgrade "$wheel"

install -D -m 0644 "$source_dir/sysusers/rpi-streamer.conf" \
    /usr/lib/sysusers.d/rpi-streamer.conf
install -D -m 0644 "$source_dir/tmpfiles/rpi-streamer.conf" \
    /usr/lib/tmpfiles.d/rpi-streamer.conf
systemd-sysusers /usr/lib/sysusers.d/rpi-streamer.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/rpi-streamer.conf

install -d -m 0755 /etc/rpi-streamer
if [ ! -e /etc/rpi-streamer/rpi-streamer.ini ]; then
    install -m 0644 "$source_dir/config/rpi-streamer.ini" \
        /etc/rpi-streamer/rpi-streamer.ini
fi
install -m 0644 "$source_dir/systemd/rpi-streamer.service" \
    /etc/systemd/system/rpi-streamer.service

install -d -m 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled
sed \
    -e "s|__LISTEN__|$listen|g" \
    -e "s|__SITE_ROOT__|/var/lib/rpi-streamer/site/|g" \
    -e "s|__MEDIA_ROOT__|/mnt/anime/|g" \
    "$source_dir/nginx/rpi-streamer.conf.template" \
    > /etc/nginx/sites-available/rpi-streamer.conf
ln -sfn /etc/nginx/sites-available/rpi-streamer.conf \
    /etc/nginx/sites-enabled/rpi-streamer.conf

if id www-data >/dev/null 2>&1; then
    usermod -a -G rpi-streamer www-data
fi
nginx -t
systemctl daemon-reload

echo "RPi Streamer installed."
echo "Review /etc/rpi-streamer/rpi-streamer.ini and the Nginx listen address."
echo "Then run: sudo systemctl enable --now rpi-streamer nginx"
