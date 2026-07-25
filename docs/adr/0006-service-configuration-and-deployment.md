# ADR 0006: Shared configuration and service lifecycle

- Status: Accepted
- Date: 2026-07-25

## Context

Native systemd and Compose deployments must scan and serve the same media root.
Early hardcoded paths and assumed virtual-environment names made installation
and upgrades fragile.

## Decision

Configuration precedence is command-specific CLI option, environment, INI,
then built-in default. It is resolved and validated once at startup. Native
Nginx configuration is rendered from the same `media_root` and `site_dir`.
Container-internal paths are fixed while host mounts remain configurable.

The service performs one scan at startup and periodically thereafter. `SIGHUP`
requests a serialized rescan; `SIGINT` and `SIGTERM` request graceful shutdown.
Root Make targets operate from the repository root, build with the caller's
selected Python, discover the installed service executable during updates, and
preserve existing INI and state. The service uses a dedicated account and
systemd hardening; containers run without root privileges or writable media.

## Consequences

The same application behavior is available natively and in containers without
assuming a particular venv name. Existing configuration is never silently
overwritten, so operators must edit changed defaults explicitly. Installation
must reject environments the service account cannot traverse.
