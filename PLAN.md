# RPi Streamer implementation plan

This plan turns the design in [README.md](README.md) into small, verifiable
milestones. Each implementation step ends with tests, documentation updates,
and one focused commit. Status values are **Pending**, **In progress**,
**Blocked**, and **Done**.

## Status

| Step | Milestone | Status | Completion evidence |
|---:|---|---|---|
| 0 | Architecture and project plan | Done | `README.md` and `PLAN.md` define the initial design |
| 1 | Python project skeleton and configuration | Done | Installable CLI, strict validation, and 22 tests; Ruff/mypy/pytest pass |
| 2 | SQLite schema and persistence layer | Done | Schema v1 repository, migrations, rollback, relations, and stale queries; 34 tests pass |
| 3 | Filesystem scanner and reconciliation | Done | Read-only fixture scans, identity moves, partial reconciliation, and 46 tests pass |
| 4 | Metadata provider and matching | Done | Conditional Jikan cache, deterministic matching, offline mocks, and 60 tests pass |
| 5 | Static catalogue generator | Done | Escaped deterministic pages, atomic rollback, and 70 offline tests pass |
| 6 | Service loop, signals, and observability | Done | Monotonic scheduling, signals, locking, JSON health, and 81 offline tests pass |
| 7 | Nginx streaming configuration | Done | Hardened template and conditional range/seek integration suite; 82 offline tests pass |
| 8 | Native packaging and systemd deployment | Done | Wheel installer, hardened unit, account/state declarations, and deployment audits |
| 9 | Container images and Compose deployment | Done | Two non-root images, hardened Compose stack, health probes, and live fixture pass |
| 10 | Deployment feedback remediation | Done | Resilient matching, one-player navigation, config-rendered Nginx, and Make lifecycle; 98 tests pass |
| 11 | End-to-end hardening and first release | In progress | Release-candidate automation/docs complete; Raspberry Pi and amd64 host acceptance pending |

## Decisions recorded

1. **Static pages before FastAPI.** Nginx can serve the catalogue and videos
   without an additional request-time Python process. Dynamic endpoints are
   deferred until a concrete feature requires them.
2. **Nginx serves media directly.** The Python application never proxies MP4
   bodies. Standard HTTP byte-range behavior provides browser streaming and
   seeking; no HLS or transcoding is planned.
3. **Jikan v4 is the initial provider.** It is read-only and requires no user
   authentication. Provider code remains behind an interface so it can be
   disabled or replaced.
4. **SQLite is the source of catalogue state.** Generated HTML is disposable
   output. Original media remains read-only.
5. **`SIGHUP` means rescan.** `SIGINT` and `SIGTERM` retain their conventional
   graceful-shutdown behavior.
6. **Configuration precedence is CLI (where offered), environment, INI,
   defaults.** The resolved configuration is validated once at startup.
7. **Ambiguous matches require an override.** The service will not silently
   replace a low-confidence title match. A per-title sidecar can pin a MAL ID.
8. **No video hashing by default.** Normalized paths, sizes, and modification
   times make rescans cheap on Raspberry Pi storage.
9. **One static player per title page.** A small, dependency-free JavaScript
   controller changes the selected local MP4 in one player; Nginx remains the
   only request-time server.
10. **Deployment paths come from resolved configuration.** Native Nginx
    generation must use the same `media_root` as the indexer instead of a
    separately hardcoded value.
11. **Build tooling uses the caller's interpreter.** Root-level Make targets
    resolve the active Python interpreter and allow an explicit override; they
    must not assume a virtual-environment name. The service executable remains
    explicit and is validated for access by the system account.
12. **Model-assisted inference stays optional.** A future OpenAI fallback may
    propose normalized titles or search hints after deterministic matching
    fails, but must use structured output, bounded calls, cached results, and a
    protected environment/credential secret. It may not silently override a
    pinned MAL ID or treat an inferred ID as verified metadata.

## Step 0 — Architecture and project plan

**Status: Done**

- Define scope, non-goals, deployment modes, configuration contract, service
  lifecycle, metadata strategy, and security boundary.
- Establish the milestone table and per-step documentation/commit workflow.
- Preserve the existing Apache-2.0 license.

**Delivered:** `README.md`, `PLAN.md`.

## Step 1 — Python project skeleton and configuration

**Status: Done**

Create the installable application and stable configuration surface.

- Add a `src/rpi_streamer/` package, `pyproject.toml`, console entry point,
  supported Python version, and a minimal dependency set.
- Prefer the standard library for INI parsing, logging, signals, paths, and
  SQLite. Add third-party packages only where they materially reduce risk
  (expected candidates: an HTTP client and a templating engine).
- Implement typed settings with defaults and precedence:
  command-specific CLI option, `RPI_STREAMER_*`, selected INI file,
  application default.
- Parse duration values and booleans consistently.
- Validate absolute and distinct paths, media-root readability, writable state
  directories, provider names, and safe scan intervals.
- Add `serve`, `scan`, and `validate-config` CLI commands with useful exit
  codes and `--help`.
- Add an example INI file and environment-variable reference.
- Establish linting, formatting, static analysis, and unit-test commands.

**Tests and acceptance**

- Table-driven tests cover defaults, every override, precedence, malformed
  durations/booleans, and invalid paths.
- `validate-config` prints a redacted, normalized configuration and exits
  nonzero for invalid input.
- Installation into a clean virtual environment exposes `rpi-streamer`.

**Documentation/commit:** replace proposed configuration language in the
README with verified usage; mark Step 1 Done; commit as
`feat: scaffold application and configuration`.

**Delivered:** a Python 3.11+ `src/` package with no runtime dependencies;
editable packaging and the `rpi-streamer` console script; typed INI/environment
settings; `serve`, `scan`, and `validate-config` command surfaces; normalized
JSON validation output and exit codes; an example INI; Ruff, mypy, and pytest
configuration; and table-driven configuration/CLI tests. `serve` and `scan`
intentionally return an unavailable status until their implementation steps.

## Step 2 — SQLite schema and persistence layer

**Status: Done**

Build a small repository layer without adopting an ORM unless migrations prove
unreasonably complex.

- Define normalized tables for schema versions, library entries, media files,
  provider records, aliases, genres, relations, artwork, and scan runs.
- Decide which raw provider response fields must be retained for re-rendering
  and diagnostics without unnecessary duplication.
- Add ordered, transactional, forward-only migrations.
- Enable foreign keys and busy timeout. Enable WAL when safe, with a documented
  fallback for filesystems that do not support it reliably.
- Provide transaction boundaries for scan reconciliation and metadata updates.
- Store timestamps in UTC and paths in a canonical relative form.
- Add query methods required by the scanner and renderer; do not expose raw SQL
  throughout the application.

**Tests and acceptance**

- A fresh database migrates to the latest version.
- Re-running migrations is idempotent; a future/unknown schema is rejected.
- CRUD, constraints, rollback, relations, and stale-record queries pass against
  temporary databases.
- The schema does not store absolute media URLs or video content.

**Documentation/commit:** document the implemented data model and backup
considerations; mark Step 2 Done; commit as
`feat: add versioned SQLite catalogue`.

**Delivered:** schema version 1 with normalized catalogue, media, provider,
alias, genre, relation, artwork, and scan-run tables; an ORM-free typed
repository; canonical relative paths and UTC timestamps; foreign keys, busy
timeout, WAL negotiation, transactional migrations, nested savepoints,
reconciliation and stale-cache queries; plus migration, CRUD, constraint,
cascade, replacement, rollback, relation, scan-run, and path tests.

## Step 3 — Filesystem scanner and reconciliation

**Status: Done**

Discover the local collection efficiently and safely.

- Walk `media_root` without following escaping symlinks.
- Treat supported extensions case-insensitively, initially `.mp4`.
- Group files into title folders, derive candidate titles from folder names,
  and natural-sort media filenames.
- Record relative paths, size, modification time, and a stable local identity.
- Parse common episode hints (`01`, `S01E01`, ranges/specials) conservatively;
  retain the original filename as the authoritative label.
- Read a documented per-title `rpi-streamer.ini` sidecar for metadata pins and
  display overrides.
- Reconcile additions, modifications, moves where safely detectable, and
  removals in one successful scan.
- Mark missing records unavailable. Do not delete remote/history state during
  ordinary scans.
- Do not let one unreadable directory invalidate the entire previous
  catalogue; report partial-scan status distinctly.

**Tests and acceptance**

- A synthetic nested library covers Unicode, spaces, URL-special characters,
  uppercase extensions, symlinks, unreadable paths, and malformed sidecars.
- Two unchanged scans produce no catalogue changes.
- Add/change/remove scenarios update only expected rows.
- Scanning never writes inside `media_root`.

**Documentation/commit:** publish the actual naming and sidecar rules; mark
Step 3 Done; commit as `feat: scan and reconcile media libraries`.

**Delivered:** a standard-library, read-only scanner; case-insensitive MP4
discovery; natural ordering and conservative episode hints; strict per-title
sidecars; schema v2 filesystem identities; safe file/title move detection;
transactional availability reconciliation; partial-scan protection and scan
summaries; an operational one-shot `scan` CLI; and fixture/unit coverage for
Unicode, spaces, URL-special characters, uppercase extensions, symlinks,
malformed sidecars, idempotency, moves, changes, removals, and read-only media
behavior. Ruff, formatting, mypy, and all 46 tests pass.

## Step 4 — Metadata provider and matching

**Status: Done**

Enrich local titles while remaining functional offline.

- Define a provider interface for search, title details, episode information,
  relations, genres, artwork references, and cache validators.
- Implement Jikan v4 using explicit timeouts, a descriptive user agent,
  conservative rate limiting (below documented limits), bounded exponential
  backoff, and handling for `304`, `429`, and transient `5xx` responses.
- Cache normalized records and enough raw response data to diagnose mappings.
- Use `ETag`/`Last-Modified` on refresh and apply configurable staleness rules.
- Implement deterministic title normalization and scored candidate matching.
- Set a confidence threshold; leave ambiguous entries unmatched and visible.
- Honor pinned MAL IDs and disabled metadata in per-title sidecars.
- Fetch/cache artwork with MIME, size, and response limits; use placeholders
  after failure.
- Expose provider errors in scan summaries without failing local discovery.

**Tests and acceptance**

- All network tests use a fake HTTP server or recorded, sanitized fixtures; CI
  does not depend on Jikan availability.
- Tests cover cache hits, `304`, throttling, retry exhaustion, malformed JSON,
  oversized artwork, ambiguous search, pinned IDs, and offline operation.
- A manual opt-in smoke test can query the live provider responsibly.

**Documentation/commit:** document provider attribution, refresh/matching
behavior, overrides, and limitations; mark Step 4 Done; commit as
`feat: enrich titles with cached anime metadata`.

**Delivered:** a typed provider interface and synchronous standard-library
Jikan v4 client; one-request-per-second throttling, explicit timeouts, bounded
retry/backoff, conditional `304` refreshes, payload and artwork limits;
deterministic Unicode-aware confidence matching with ambiguity rejection;
pinned/disabled sidecar behavior; schema v3 normalized provider episodes;
transactional detail, alias, genre, relation, episode, raw-response, validator,
and artwork persistence; missing-art markers; fresh-cache and offline
operation; per-title errors in partial scan summaries; and an opt-in live
smoke test. Ruff, formatting, mypy, and 60 offline tests pass; the live test is
skipped unless explicitly enabled.

## Step 5 — Static catalogue generator

**Status: Done**

Generate a useful, accessible catalogue from SQLite.

- Add compact templates and local CSS; avoid a frontend build tool and CDN.
- Render a home/title index, title details, genre indexes, relationship links,
  breadcrumbs, scan timestamp/status, and unmatched-title indicators.
- List only local files as playable episodes while showing provider episode
  context separately where useful.
- Render an HTML5 video player with `preload="metadata"`.
- Correctly URL-encode media paths and HTML-escape filenames/provider content.
- Generate stable, collision-resistant page slugs independent of display names.
- Copy validated cover art into the generated tree.
- Write into a sibling staging directory, validate required output, then
  atomically publish it while retaining/recovering the previous good build.
- Make output deterministic for unchanged catalogue data.

**Tests and acceptance**

- Snapshot/DOM tests cover complete, unmatched, offline, Unicode, missing-art,
  and related-title fixtures.
- Security tests cover HTML injection, path traversal, malformed remote URLs,
  and slug collisions.
- A failed render leaves the previously published site intact.
- Pages work without JavaScript and meet basic keyboard/semantic HTML checks.

**Documentation/commit:** add screenshots or HTML examples and navigation
details; mark Step 5 Done; commit as
`feat: generate the static media catalogue`.

**Delivered:** packaged standard-library templates and local responsive CSS;
home, title, genre, relationship, breadcrumb, scan-status, local-player, and
provider-context views; unmatched and missing-art states; stable identity
slugs and hashed genre slugs; per-segment media URL encoding and HTML escaping;
validated local cover copies without remote image URLs; deterministic output;
sibling staging validation, atomic publication, retained previous builds, and
failure cleanup/recovery; generation in the one-shot scan workflow; and
render/security tests for complete, offline, Unicode, injection, traversal,
slug collision, accessibility, and rollback cases. Ruff, formatting, mypy,
and 70 offline tests pass; the opt-in live Jikan test remains skipped by
default.

## Step 6 — Service loop, signals, and observability

**Status: Done**

Turn one-shot components into a reliable long-running indexer.

- Scan immediately on startup, then on a monotonic interval.
- Coalesce `SIGHUP` requests and trigger a follow-up scan if a signal arrives
  during an active scan.
- Handle `SIGINT`/`SIGTERM` gracefully without publishing partial output.
- Prevent overlapping scans within one process and guard against accidental
  multiple indexer instances sharing a state directory.
- Add structured, journald-friendly logs with scan IDs and summaries, but no
  full remote payloads or control characters.
- Define useful exit codes and optional machine-readable one-shot summaries.
- Add a lightweight health/status artifact consumed by deployment health
  checks.

**Tests and acceptance**

- Fake-clock tests cover intervals and disabled scheduling.
- Process-level tests verify `SIGHUP`, repeated signals, termination during
  idle and active work, lock contention, and recovery after failed scans.
- Idle CPU use is negligible and memory use is measured on the target Pi.

**Documentation/commit:** document operational commands, logs, signals, and
failure behavior; mark Step 6 Done; commit as
`feat: run periodic and signal-triggered scans`.

**Delivered:** a shared scan/enrich/generate pipeline; immediate startup scans;
monotonic interval scheduling with an indefinite disabled state; coalesced
`SIGHUP` follow-ups; graceful `SIGINT`/`SIGTERM`; an advisory state-directory
process lock shared by `serve` and `scan`; concise structured summaries and
sanitized error logs; documented exit codes and one-shot `--json` output; and
an atomically published `status.json` health artifact. Fake-clock, signal,
failure recovery, lock lifecycle, status, CLI, and real subprocess
`SIGHUP`/`SIGTERM` tests are included. Ruff, formatting, mypy, and 81 offline
tests pass; the opt-in live Jikan test remains skipped. Idle waiting is
event-based and consumes no polling CPU; target-Pi RSS measurement remains a
deployment validation item because it cannot be measured on the development
host.

## Step 7 — Nginx streaming configuration

**Status: Done**

Serve catalogue assets and media efficiently without exposing other paths.

- Add a parameterized Nginx site template for the generated root and media
  alias, with correct trailing-slash semantics.
- Configure MP4 MIME type, normal byte ranges, sendfile behavior suitable for
  local disks, conditional requests, and conservative open-file caching.
- Do not add `mp4` pseudo-streaming directives unless testing identifies a
  real compatibility need; browsers should use standard ranges.
- Prevent directory listing, dotfile access, path traversal, and unintended
  symlink escape.
- Add a small health endpoint/artifact and practical cache policies:
  revalidate HTML, cache versioned artwork/CSS, and allow media range requests.
- Document binding to LAN-only interfaces and firewall expectations.

**Tests and acceptance**

- `nginx -t` passes with fixture paths.
- Integration tests verify `200`, `206`, `Content-Range`, seeking into a known
  MP4 fixture, `416`, HEAD/conditional requests, Unicode filenames, and MIME.
- Traversal, dotfile, non-media, and paths outside the mount are inaccessible.
- The generated site remains browsable while the indexer is stopped.

**Documentation/commit:** add verified Nginx setup and troubleshooting; mark
Step 7 Done; commit as `feat: serve catalogue and MP4 ranges with nginx`.

**Delivered:** a three-placeholder LAN Nginx site template with correct
root/alias trailing-slash semantics; native static byte ranges and MP4 MIME;
sendfile, validators, bounded open-file caching, and route-specific cache
policies; an isolated health endpoint; and denial rules for directory indexes,
dotfiles, non-MP4 media paths, traversal, and symlinks. Generated CSS and cover
names now contain content hashes so immutable caching is safe. The test suite
renders and audits the template everywhere and, when Nginx is installed, runs
`nginx -t` plus loopback HTTP checks for `200`, byte-accurate `206`,
`Content-Range`, `416`, HEAD/conditional behavior, Unicode names, MIME, and
boundary denial. On this development host, 82 tests pass; two Nginx integration
methods are skipped because no Nginx binary is installed, and the existing
opt-in live Jikan smoke test remains skipped. Exact setup, LAN/firewall
guidance, probes, permissions, cache behavior, and troubleshooting are in the
README.

## Step 8 — Native packaging and systemd deployment

**Status: Done**

Provide a reproducible Raspberry Pi/Linux installation.

- Add a systemd unit with `ExecReload` sending `SIGHUP`, restart policy,
  readiness ordering, dedicated user/group, and state-directory creation.
- Apply compatible hardening: no new privileges, private temporary storage,
  protected system/home paths, restricted writable paths, and read-only media.
- Add example `/etc/rpi-streamer/rpi-streamer.ini`, Nginx site, tmpfiles/sysusers
  declarations or an explicit installer procedure.
- Validate Nginx/indexer group access without making the media tree world
  writable.
- Define upgrade, database backup/restore, rollback, uninstall, and log
  inspection procedures.
- Decide on distributable artifact format after testing target OS versions
  (wheel plus deployment files is the baseline).

**Tests and acceptance**

- Install onto a clean supported Raspberry Pi OS/Debian environment.
- `start`, `stop`, `restart`, `reload`, boot enablement, failure restart, and
  permissions behave as documented.
- The service runs unprivileged and writes only to declared state paths.

**Documentation/commit:** replace deployment targets with exact native install
instructions; mark Step 8 Done; commit as
`feat: add hardened systemd deployment`.

**Delivered:** a wheel-plus-deployment-files native artifact; an idempotent
installer that creates an isolated `/opt` virtual environment while preserving
existing configuration; example production INI and parameterized Nginx site;
dedicated sysusers/tmpfiles account and state declarations; and a foreground
systemd unit with reload, restart, network ordering, journald, controlled
shutdown, and a strict read-only filesystem sandbox whose only writable path
is application state. Static tests audit the unit, account, state, config, and
installer shell syntax. `systemd-analyze verify` runs conditionally; this
development sandbox blocks its communication socket, so the test records a
skip here. The README now gives exact Debian 12/Raspberry Pi OS Bookworm wheel
installation, non-world-readable group permissions, lifecycle, diagnostics,
upgrade, backup/restore, forward-migration rollback, and conservative uninstall
procedures. Full boot/restart and permission behavior remains an operator
acceptance check on a real systemd target because containers and this WSL
development host do not boot systemd as PID 1.

## Step 9 — Container images and Compose deployment

**Status: Done**

Package the same application/config contract for containers.

- Build minimal, pinned Python and Nginx images with reproducible dependency
  installation and non-root processes where supported.
- Add Compose services, read-only media mounts, persistent state/site volume,
  environment configuration, health checks, and signal forwarding.
- Avoid privileged mode, host PID/network namespaces, Docker socket mounts,
  and writable application filesystems beyond explicit volumes.
- Ensure the indexer and Nginx agree on container-internal paths while allowing
  arbitrary host mount locations.
- Add multi-platform build metadata for `linux/amd64` and `linux/arm64`.
- Include a `.dockerignore` and image provenance/version labels.

**Tests and acceptance**

- A fresh `docker compose up` scans a fixture collection and streams a file.
- Restart preserves SQLite/artwork/site state.
- `docker compose kill -s HUP indexer` triggers a scan.
- Container health, clean shutdown, read-only mount behavior, and architecture
  builds pass.

**Documentation/commit:** add exact Compose configuration, upgrades, and volume
ownership guidance; mark Step 9 Done; commit as
`feat: add container deployment`.

**Delivered:** pinned Python and Nginx multi-stage images with non-root runtime
users and OCI labels; a two-service Compose stack with read-only roots/media,
an explicit persistent state volume, tmpfs scratch space, dropped capabilities,
health checks, signal forwarding, and environment overrides; a Buildx Bake
definition for `linux/amd64` and `linux/arm64`; and a focused container asset
audit. A live Docker fixture verified initial scanning, catalogue serving, MP4
byte ranges, state persistence across restart, `SIGHUP` rescanning, read-only
media, healthy processes, and graceful Compose lifecycle. The actual
multi-platform push remains a registry/Buildx operator acceptance step.

## Step 10 — Deployment feedback remediation

**Status: Done**

Address issues found on the first Raspberry Pi deployment. Implement the
following substeps in order; keep the row above **Pending** until all four
substeps and the upgrade exercise pass.

| Substep | Change | Status | Acceptance evidence |
|---:|---|---|---|
| 10.1 | Metadata search resilience and diagnostics | Done | Okinawa regression fixture matches MAL ID 55842; bounded fallback and outcomes tested |
| 10.2 | Single-player episode navigation | Done | One player, selector, previous/next controls, fragment, and no-JS links generated |
| 10.3 | Config-driven native Nginx media root | Done | `/mnt/media` and spaced paths render from resolved config; unsafe paths fail |
| 10.4 | Root Make install and update workflow | Done | Root Make targets use selected Python, consistent artifacts, backup, and conservative uninstall |

### 10.1 — Metadata search resilience and diagnostics

- Reproduce the reported folder name
  `Okinawa de Suki ni Natta Ko ga Hougen Sugite Tsurasugi` with captured,
  offline Jikan search responses. Verify whether the failure is an empty search,
  a score below the confidence threshold, or an ambiguity-margin rejection.
- Add bounded search fallbacks for long/near-canonical titles. Candidate
  approaches include retrying normalized word prefixes and ranking the union of
  results. Do not globally lower the confidence threshold in a way that creates
  silent false matches.
- Score canonical, English, Japanese, and synonym aliases consistently. Add
  regression coverage for the one-character suffix difference between
  `Tsurasugi` and MAL's `Tsurasugiru`, expecting provider ID `55842`.
- Record a concise match outcome for each title: matched ID and score, no search
  results, below threshold, ambiguous candidates, provider failure, or pinned
  sidecar. Surface it in scan logs/status without storing entire remote
  responses or unsafe terminal text.
- Document `.rpi-streamer.ini` `mal_id = 55842` as the immediate deterministic
  override and ensure a new/changed sidecar invalidates a previous unmatched
  decision on the next rescan.

**Tests and acceptance**

- Offline tests cover the reported title, exact aliases, close competing
  candidates, empty results, fallback request limits, and provider failures.
- A fixture initially unmatched becomes enriched after adding a `mal_id`
  sidecar or after the improved search succeeds.
- Ordinary tests remain network-free and Jikan throttling/retry bounds remain
  intact.

### 10.2 — Single-player episode navigation

- Replace the repeated `<video>` elements with one player and one escaped data
  model containing only locally available MP4 files.
- Add a native `<select>` episode picker plus Previous and Next buttons. Update
  the player source, heading, button disabled states, URL fragment, and document
  state without reloading the page.
- Keep controls keyboard accessible, give them explicit labels, announce the
  selected episode, and preserve the browser's native video controls.
- Ship a small dependency-free JavaScript asset with the generated static site;
  use no CDN or inline remote code. On navigation, pause the old source, change
  it, call `load()`, and do not autoplay.
- Provide a useful no-JavaScript fallback: the first local episode remains
  playable and links for the remaining episodes remain available. Keep provider
  episode metadata separate from local playability.
- Apply responsive styling so the player and controls remain usable on phones
  and televisions.

**Tests and acceptance**

- Generated HTML contains exactly one `<video>` for zero, one, and many-file
  fixtures as applicable, and all filenames/URLs remain escaped.
- DOM-level or browser tests verify selector and previous/next behavior,
  first/last disabled states, fragment deep-linking, and filenames with Unicode
  or punctuation.
- Existing HTTP range/seek behavior continues to return `206`, and a manual
  browser check confirms switching episodes does not download every MP4.

### 10.3 — Config-driven native Nginx media root

- Remove `/mnt/anime` substitution from the native installer. Render the Nginx
  alias from the same fully resolved `media_root` used by the Python service.
- Add a non-mutating deployment/render command or helper that reads normal
  CLI/environment/INI precedence, validates the absolute path, escapes it for
  Nginx safely, preserves the required trailing slash, and writes a candidate
  configuration atomically.
- Make install/update run `nginx -t` against the candidate before replacing the
  active site. Preserve the last working Nginx configuration on validation or
  reload failure.
- Decide and document how environment-only `media_root` overrides are persisted
  for systemd and Nginx; reject a transient mismatch rather than letting the
  scanner and server use different roots.
- Update examples and permission checks from a hardcoded `/mnt/anime` to the
  configured value, including the reported `/mnt/media` deployment.

**Tests and acceptance**

- Paths containing spaces and Nginx-significant characters are either rendered
  safely or rejected with an actionable error.
- Installer tests prove that changing `media_root` from `/mnt/anime` to
  `/mnt/media` updates Nginx without touching the media collection.
- Nginx syntax, traversal protection, MP4-only access, and byte-range tests pass
  for the configured root.

### 10.4 — Root Make install and update workflow

- Add a small root `Makefile` with discoverable `help`, `build`, `check`,
  `install`, `update`, `validate`, `restart`, and `uninstall` targets. All
  relative paths must be rooted at the repository, so commands work from the
  documented repository root and consistently use `deployment/dist/` (or one
  other single documented artifact directory).
- Resolve `PYTHON` from the caller's active environment, defaulting to the
  current `python3`, and allow `make ... PYTHON=/absolute/path/to/python`.
  Never assume `$HOME/.venvs/py-rpistreamer`, `.venv`, or another environment
  name; do not activate environments inside Make.
- Separate unprivileged wheel creation/package installation from privileged
  system-file installation so `sudo` does not silently replace the selected
  interpreter. Pass the resolved console-script path explicitly into the
  systemd unit rendering.
- Support a venv, an explicitly selected system interpreter where its packaging
  policy permits installation, and an already installed console script. Detect
  PEP 668/read-only environments and fail with remediation instead of using
  `--break-system-packages`.
- Before changing the host, preflight the executable, configuration, service
  account access, Nginx availability, and media path. In particular, detect a
  selected executable cannot be traversed by the service account.
  `ProtectHome=read-only` permits a caller-selected home venv without allowing
  service writes, but ordinary Unix permissions still apply.
- Make `install` idempotent and preserve existing INI/state. Make `update`
  create a state/config backup, install the new wheel and deployment assets,
  validate configuration and Nginx, then restart. On failure, restore the
  previous deployment assets and leave clear rollback instructions for any
  forward-only database migration.
- Keep destructive uninstall/state removal separate and explicit. The default
  uninstall must retain `/etc/rpi-streamer`, `/var/lib/rpi-streamer`, and media.

**Tests and acceptance**

- Shell/Make tests cover invocation from the repository root, paths with spaces,
  `PYTHON` overrides, active-venv resolution, preserved config, repeated
  install, update, failed preflight, and failed Nginx validation.
- A clean Raspberry Pi OS install needs only the documented OS prerequisites,
  `make install` variables, config review, and service start.
- An existing Step 8 installation upgrades through `make update`; its SQLite
  catalogue, generated site, configured `/mnt/media` root, and service
  enablement survive.
- README commands are copied into an automated smoke test so documented paths
  cannot drift again.

**Documentation/commit:** document the matching override and diagnostics,
episode controls, resolved media path, Make variables, clean install, update,
rollback, and legacy-upgrade workflow. Mark Step 10 and all substeps Done only
after the deployed Raspberry Pi acceptance check; commit as
`fix: address initial deployment feedback`.

**Delivered:** high-confidence matching now performs at most one long-title
prefix retry and emits actionable match outcomes; the reported Okinawa name
has a MAL `55842` regression test and documented sidecar override. Generated
title pages contain one player with accessible selector/previous/next controls,
fragment selection, escaped URLs, and no-JavaScript links. A packaged CLI
renderer produces Nginx configuration from resolved `media_root` and
`site_dir`, with atomic writes, path validation, and installer rollback after
failed syntax checks. Root Make targets build into `deployment/dist`, use the
caller's `PYTHON`, pass the selected executable across sudo, back up before
updates, validate, restart, and conservatively uninstall. Ninety-eight offline
tests pass; conditional systemd/Nginx/live-Jikan checks remain environment
dependent. The supplied private-LAN host was unreachable from the execution
environment, and the single live Jikan diagnostic returned HTTP 504, so the
deployed browser/update acceptance should be repeated on the Raspberry Pi.

The suggested GPT-5.6 Luna inference fallback is intentionally deferred to a
separate credentialed extension: no API key was available to exercise it, and
secrets must not be stored in the ordinary INI. It should use the Responses API
with strict structured output only after deterministic Jikan matching fails.

## Step 11 — End-to-end hardening and first release

**Status: In progress**

Close cross-component gaps and prepare a maintainable first release.

- Run an end-to-end fixture through scan, metadata mock, SQLite, generation,
  Nginx, browser-style range requests, rescan, and removal.
- Test power-loss-style interruption at database and publish boundaries.
- Profile a representative large library on Raspberry Pi hardware; set
  reasonable performance budgets and fix major regressions.
- Audit dependency licenses, pinning/update policy, remote-content handling,
  filesystem boundaries, logs, default binding, and container/systemd
  hardening.
- Add CI for lint, typing, unit/integration tests, package build, Nginx config,
  container build, and architecture coverage where runners permit.
- Establish semantic versioning, changelog, support matrix, contribution guide,
  and release checklist.
- Record known limitations and deferred features (dynamic API, search index,
  optional model-assisted title inference, other metadata providers, non-MP4
  formats).

**Tests and acceptance**

- All earlier acceptance criteria pass from a clean checkout.
- A documented disaster-recovery exercise restores database and generated
  output.
- The release artifact installs and streams on at least one supported Raspberry
  Pi and one amd64 Linux host.

**Documentation/commit:** update all docs to describe shipped behavior, mark
Step 11 Done, create a changelog entry, and commit as
`chore: prepare initial release`.

**Release-candidate work delivered:** version 0.1.0rc1 metadata and an
unreleased 0.1.0 changelog;
Python 3.11–3.13 CI for formatting, linting, strict typing, offline tests,
wheel builds and artifacts; Nginx syntax/range integration CI; amd64 container
build CI; a deterministic end-to-end fixture spanning scan, mocked metadata,
SQLite, generation, rescan, and removal; and a repository-root acceptance
target. Existing transaction rollback and failed-publication tests cover
power-loss-style atomicity boundaries. The dependency, remote-content,
filesystem, logging, binding, systemd, and container audit is recorded in the
security guide. The support matrix, semantic-version policy, contribution
guide, known limitations, deferred features, disaster-recovery procedure, and
release checklist are documented.

**Remaining before Done:** run and record the release checklist on one
Raspberry Pi OS Bookworm arm64 host and one supported amd64 Linux host,
including install/stream/seek/rescan/recovery evidence. Profile a representative
library on the Raspberry Pi, record cold/warm duration and peak RSS, and set a
numeric performance budget from that measurement. Once those checks pass,
mark Step 11 Done and create the `chore: prepare initial release` commit/tag.

**Raspberry Pi update feedback:** the first arm64 update attempt safely stopped
when the active development environment under `/home/pi` was not traversable
by the service account. Updates now discover and upgrade the executable from
the installed systemd unit, preserve the installed Nginx listener by default,
and use the caller's selected Python only to build the wheel. Explicit
`SERVICE_EXECUTABLE` and `LISTEN` values still override discovery. The
installer explains why private home environments cannot run as the service and
does not weaken home-directory permissions. A follow-up arm64 attempt confirmed
the installed `/opt/rpi-streamer/venv` was discovered, then exposed that this
legacy environment is root-owned; update installation into the discovered
production interpreter now runs through `sudo` while wheel construction
remains unprivileged. The journal then revealed a transient `episode_script`
generation failure caused by the backup helper restarting the indexer before
the package update finished. The update is now one failure-safe transaction:
it preserves prior active state, stops the indexer across backup/build/install,
and restarts it via a shell trap on failure. Partial scans also log their
bounded, sanitized issue summary instead of reporting only an error count.

## Cross-cutting quality rules

These apply to every milestone:

- Keep runtime dependencies few, pinned within a documented update policy, and
  justified in review.
- Do not perform network access during ordinary unit tests.
- Use temporary directories and small synthetic media fixtures in tests; never
  require a personal collection.
- Treat paths, filenames, sidecar values, and provider data as untrusted input.
- Keep media mounts read-only to Python and containers.
- Preserve the last known-good database transaction and generated site after
  failures.
- Add migration and behavior tests before changing persisted or public
  contracts.
- Log actionable context without leaking complete remote responses or emitting
  terminal-control characters.
- Update `README.md`, this status table, the relevant step notes, and tests in
  the same commit as each implemented milestone.

## Definition of done for a step

A step may move to **Done** only when:

1. its scoped implementation and acceptance tests are complete;
2. relevant automated checks pass, or a documented environment limitation
   explains any check that could not run;
3. README instructions describe current, verified behavior rather than plans;
4. this plan records status and any material decision changes;
5. `git diff` contains no accidental generated, personal, or unrelated files;
6. the step is committed with a descriptive message and clean handoff notes.

If work is partially complete, keep the step **In progress** and list the
remaining acceptance criteria rather than marking it Done.
