#!/bin/sh
set -eu

usage() {
    echo "usage: sudo deployment/install.sh WHEEL [LISTEN [EXECUTABLE [MEDIA_ROOT]]]" >&2
    echo "example: sudo deployment/install.sh deployment/dist/rpi_streamer-*.whl 192.168.1.20:8080 '' /mnt/media" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 4 ]; then
    usage
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "rpi-streamer installer: run as root (normally through sudo)" >&2
    exit 2
fi

wheel=$1
listen=${2:-127.0.0.1:8080}
executable=${3:-}
media_root=${4:-/mnt/anime}
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
case "$media_root" in
    /*) ;;
    *)
        echo "rpi-streamer installer: media root must be absolute" >&2
        exit 2
        ;;
esac
case "$media_root" in
    *[\&\|\;\`\'\"\\]*)
        echo "rpi-streamer installer: media root contains unsafe characters" >&2
        exit 2
        ;;
esac

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for command in python3 systemd-sysusers systemd-tmpfiles nginx runuser; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "rpi-streamer installer: required command not found: $command" >&2
        exit 3
    fi
done

install -d -m 0755 /opt/rpi-streamer
if [ -z "$executable" ]; then
    if [ ! -x /opt/rpi-streamer/venv/bin/python ]; then
        python3 -m venv /opt/rpi-streamer/venv
    fi
    /opt/rpi-streamer/venv/bin/python -m pip install --upgrade "$wheel"
    executable=/opt/rpi-streamer/venv/bin/rpi-streamer
fi
case "$executable" in
    /*) ;;
    *)
        echo "rpi-streamer installer: executable must be an absolute path" >&2
        exit 2
        ;;
esac
case "$executable" in
    *[\&\|\;\`\'\"\\[:space:]]*)
        echo "rpi-streamer installer: executable path contains unsafe characters" >&2
        exit 2
        ;;
esac
if [ ! -x "$executable" ]; then
    echo "rpi-streamer installer: executable is not runnable: $executable" >&2
    exit 2
fi

install -D -m 0644 "$source_dir/sysusers/rpi-streamer.conf" \
    /usr/lib/sysusers.d/rpi-streamer.conf
install -D -m 0644 "$source_dir/tmpfiles/rpi-streamer.conf" \
    /usr/lib/tmpfiles.d/rpi-streamer.conf
systemd-sysusers /usr/lib/sysusers.d/rpi-streamer.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/rpi-streamer.conf
if ! runuser -u rpi-streamer -- test -x "$executable"; then
    echo "rpi-streamer installer: service account cannot execute $executable" >&2
    echo "choose an environment in a directory traversable by rpi-streamer" >&2
    exit 3
fi

install -d -m 0755 /etc/rpi-streamer
if [ ! -e /etc/rpi-streamer/rpi-streamer.ini ]; then
    sed "s|^media_root = .*|media_root = $media_root|" \
        "$source_dir/config/rpi-streamer.ini" \
        > /etc/rpi-streamer/rpi-streamer.ini
    chmod 0644 /etc/rpi-streamer/rpi-streamer.ini
fi
unit=/etc/systemd/system/rpi-streamer.service
unit_backup=/etc/systemd/system/rpi-streamer.service.previous
if [ -e "$unit" ]; then
    cp -p "$unit" "$unit_backup"
fi
sed "s|/opt/rpi-streamer/venv/bin/rpi-streamer|$executable|g" \
    "$source_dir/systemd/rpi-streamer.service" \
    > "$unit"

install -d -m 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled
candidate=$(mktemp /etc/nginx/sites-available/.rpi-streamer.XXXXXX)
backup=/etc/nginx/sites-available/rpi-streamer.conf.previous
trap 'rm -f "$candidate"' EXIT
"$executable" --config /etc/rpi-streamer/rpi-streamer.ini render-nginx \
    --listen "$listen" --output "$candidate"
if [ -e /etc/nginx/sites-available/rpi-streamer.conf ]; then
    cp -p /etc/nginx/sites-available/rpi-streamer.conf "$backup"
fi
mv "$candidate" /etc/nginx/sites-available/rpi-streamer.conf
trap - EXIT
ln -sfn /etc/nginx/sites-available/rpi-streamer.conf \
    /etc/nginx/sites-enabled/rpi-streamer.conf

if id www-data >/dev/null 2>&1; then
    usermod -a -G rpi-streamer www-data
fi
if ! nginx -t; then
    if [ -e "$backup" ]; then
        mv "$backup" /etc/nginx/sites-available/rpi-streamer.conf
    fi
    if [ -e "$unit_backup" ]; then
        mv "$unit_backup" "$unit"
    fi
    echo "rpi-streamer installer: Nginx validation failed; previous site restored" >&2
    exit 3
fi
systemctl daemon-reload

echo "RPi Streamer installed."
echo "Review /etc/rpi-streamer/rpi-streamer.ini and the Nginx listen address."
echo "Then run: sudo systemctl enable --now rpi-streamer nginx"
