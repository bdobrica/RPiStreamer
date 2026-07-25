# ADR 0007: Last-known-good operation and offline verification

- Status: Accepted
- Date: 2026-07-25

## Context

The service handles untrusted filenames and remote metadata on a small
always-on host. Filesystem, provider, model, package-update, and power failures
must not remove working playback or corrupt published state.

## Decision

Database reconciliation and metadata replacement are transactional. Static
generation publishes through a validated staging directory and retains the
previous good site on failure. Updates stop the service across backup, wheel
installation, system asset replacement, and validation, with failure-safe
restart behavior. Nginx aliases are rendered and syntax-checked before
activation.

Paths, sidecars, remote JSON, model output, artwork, and generated HTML are
validated or escaped. Media is read-only, artwork has scheme/MIME/size limits,
logs sanitize control characters, native credentials use restrictive
permissions, and container capabilities are dropped.

Ordinary tests never contact external services. Provider and model behavior use
fake transports and synthetic media; opt-in live smoke tests are diagnostic
only. Persisted and public contract changes require migration and behavior
tests. Release acceptance covers both Raspberry Pi arm64 and amd64 hosts.

## Consequences

Implementation work includes staging, rollback, and failure-path tests instead
of only happy paths. Live provider availability cannot make CI flaky, while
host-specific Nginx, systemd, streaming, recovery, and performance checks
remain explicit release activities.
