PYTHON ?= python3
LISTEN ?= 127.0.0.1:8080
MEDIA_ROOT ?= /mnt/anime
DIST_DIR := $(CURDIR)/deployment/dist
SERVICE_EXECUTABLE ?= $(shell "$(PYTHON)" -c 'import sysconfig; print(sysconfig.get_path("scripts") + "/rpi-streamer")')
BACKUP_DIR ?= /var/backups/rpi-streamer

.DEFAULT_GOAL := help
.ONESHELL:
.PHONY: help build check acceptance install update backup validate restart uninstall clean

help:
	@echo "RPi Streamer targets:"
	@echo "  make check                         Run formatting, lint, typing, and tests"
	@echo "  make acceptance                    Run optional host acceptance checks"
	@echo "  make install [LISTEN=HOST:PORT]    Build and install from the repo root"
	@echo "  make update [LISTEN=HOST:PORT]     Update an existing native installation"
	@echo "  make backup                        Back up config, state, and service assets"
	@echo "  make validate                      Validate installed configuration and Nginx"
	@echo "  make restart                       Restart indexer and reload Nginx"
	@echo "Variables: PYTHON, SERVICE_EXECUTABLE, LISTEN, MEDIA_ROOT, BACKUP_DIR"

build:
	mkdir -p "$(DIST_DIR)"
	rm -f "$(DIST_DIR)"/rpi_streamer-*.whl
	"$(PYTHON)" -m pip wheel --no-deps --wheel-dir "$(DIST_DIR)" "$(CURDIR)"

check:
	"$(PYTHON)" -m ruff format --check .
	"$(PYTHON)" -m ruff check .
	"$(PYTHON)" -m mypy
	"$(PYTHON)" -m pytest

acceptance:
	"$(PYTHON)" -m pytest tests/test_end_to_end.py tests/test_nginx.py

install: build
	set -eu
	set -- "$(DIST_DIR)"/rpi_streamer-*.whl
	wheel=$$1
	test -f "$$wheel"
	"$(PYTHON)" -m pip install --upgrade "$$wheel"
	sudo "$(CURDIR)/deployment/install.sh" "$$wheel" "$(LISTEN)" "$(SERVICE_EXECUTABLE)" "$(MEDIA_ROOT)"

backup:
	sudo "$(CURDIR)/deployment/backup.sh" "$(BACKUP_DIR)"

update:
	set -eu
	was_active=false
	if systemctl is-active --quiet rpi-streamer; then
		sudo systemctl stop rpi-streamer
		was_active=true
	fi
	trap 'if [ "$$was_active" = true ]; then sudo systemctl start rpi-streamer; fi' \
		EXIT HUP INT TERM
	sudo "$(CURDIR)/deployment/backup.sh" "$(BACKUP_DIR)"
	mkdir -p "$(DIST_DIR)"
	rm -f "$(DIST_DIR)"/rpi_streamer-*.whl
	"$(PYTHON)" -m pip wheel --no-deps \
		--wheel-dir "$(DIST_DIR)" "$(CURDIR)"
	set -- "$(DIST_DIR)"/rpi_streamer-*.whl
	wheel=$$1
	test -f "$$wheel"
	service_executable="$(SERVICE_EXECUTABLE)"
	if [ "$(origin SERVICE_EXECUTABLE)" = "file" ] && \
	   [ -r /etc/systemd/system/rpi-streamer.service ]; then
		installed_executable=$$(sed -n \
			's|^ExecStart=\([^[:space:]]*\).*|\1|p' \
			/etc/systemd/system/rpi-streamer.service)
		if [ -n "$$installed_executable" ]; then
			service_executable=$$installed_executable
		fi
	fi
	test -x "$$service_executable"
	service_python=$$(sed -n '1s|^#!||p' "$$service_executable")
	if [ ! -x "$$service_python" ]; then
		echo "Cannot determine the Python interpreter for $$service_executable" >&2
		echo "Set SERVICE_EXECUTABLE to an installed rpi-streamer script." >&2
		exit 3
	fi
	sudo "$$service_python" -m pip install --upgrade "$$wheel"
	listen="$(LISTEN)"
	if [ "$(origin LISTEN)" = "file" ] && \
	   [ -r /etc/nginx/sites-available/rpi-streamer.conf ]; then
		installed_listen=$$(sed -n \
			's/^[[:space:]]*listen[[:space:]]\([^;]*\);.*/\1/p' \
			/etc/nginx/sites-available/rpi-streamer.conf)
		if [ -n "$$installed_listen" ]; then
			listen=$$installed_listen
		fi
	fi
	sudo "$(CURDIR)/deployment/install.sh" \
		"$$wheel" "$$listen" "$$service_executable" "$(MEDIA_ROOT)"
	if [ "$$was_active" = true ]; then
		sudo systemctl start rpi-streamer
		was_active=false
	fi
	sudo systemctl reload nginx
	trap - EXIT HUP INT TERM

validate:
	"$(SERVICE_EXECUTABLE)" --config /etc/rpi-streamer/rpi-streamer.ini validate-config
	sudo nginx -t

restart:
	sudo systemctl restart rpi-streamer
	sudo systemctl reload nginx

uninstall:
	sudo "$(CURDIR)/deployment/uninstall.sh"

clean:
	rm -f "$(DIST_DIR)"/rpi_streamer-*.whl
