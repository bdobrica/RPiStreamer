# Security and operations

RPi Streamer is for trusted local networks only. It has no authentication,
authorization, TLS termination, transcoding sandbox, or public-internet
hardening. Bind it to a private address, restrict it with the host firewall,
and never port-forward it.

Local paths, filenames, sidecars, and provider responses are untrusted.
Generated text is escaped, media URLs are segment-encoded, media symlinks are
refused by Nginx, remote artwork is HTTP(S)-only and size/MIME bounded, and
logs remove control characters. Python and container media mounts are
read-only; Nginx needs read/traverse access only.

There are no runtime Python dependencies in the 0.1.0 release candidate.
Development dependencies
use compatible upper bounds and are reviewed before releases. Container base
images are digest-pinned and should be refreshed deliberately after upstream
security review. CI actions are major-version pinned; review their release
notes before updates.

Configuration currently contains no secrets. If optional model-assisted
matching is implemented, credentials must use a protected environment file,
systemd credential, or container secret—not the ordinary INI, image, logs, or
repository.

Back up `/etc/rpi-streamer`, `/var/lib/rpi-streamer`, the systemd unit, and the
Nginx site configuration. The media collection is external and is not included
in application backups.
