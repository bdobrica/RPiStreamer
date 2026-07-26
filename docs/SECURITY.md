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

Optional model-assisted matching may read an OpenAI API key from the native
INI, which is installed as `root:rpi-streamer` mode `0640`, or from the
environment. The key must not appear in normalized configuration output,
SQLite, prompts, images, logs, generated pages, or the repository. Backups of
`/etc/rpi-streamer` contain the INI and must be protected as credentials.
Container deployments should prefer a runtime environment value or secret
rather than committing a key.

The planned multi-work mapping boundary, including glob, relation-graph,
candidate, and model-output controls, is documented in the
[multi-work threat model](MULTI_WORK_THREAT_MODEL.md).

Back up `/etc/rpi-streamer`, `/var/lib/rpi-streamer`, the systemd unit, and the
Nginx site configuration. The media collection is external and is not included
in application backups.
