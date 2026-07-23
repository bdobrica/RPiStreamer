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
| 3 | Filesystem scanner and reconciliation | Pending | Fixture library is scanned idempotently; change tests pass |
| 4 | Metadata provider and matching | Pending | Cached Jikan integration and manual overrides pass mocked tests |
| 5 | Static catalogue generator | Pending | Safe, deterministic pages are generated from fixture data |
| 6 | Service loop, signals, and observability | Pending | Scheduled and `SIGHUP` scans work; shutdown is graceful |
| 7 | Nginx streaming configuration | Pending | Range/seek, MIME, traversal, and static-page checks pass |
| 8 | Native packaging and systemd deployment | Pending | Clean-host install and service lifecycle are documented/tested |
| 9 | Container images and Compose deployment | Pending | Health checks pass on supported architectures |
| 10 | End-to-end hardening and first release | Pending | Full acceptance suite passes; versioned release is documented |

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

**Status: Pending**

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

## Step 4 — Metadata provider and matching

**Status: Pending**

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

## Step 5 — Static catalogue generator

**Status: Pending**

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

## Step 6 — Service loop, signals, and observability

**Status: Pending**

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

## Step 7 — Nginx streaming configuration

**Status: Pending**

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

## Step 8 — Native packaging and systemd deployment

**Status: Pending**

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

## Step 9 — Container images and Compose deployment

**Status: Pending**

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

## Step 10 — End-to-end hardening and first release

**Status: Pending**

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
  other metadata providers, non-MP4 formats).

**Tests and acceptance**

- All earlier acceptance criteria pass from a clean checkout.
- A documented disaster-recovery exercise restores database and generated
  output.
- The release artifact installs and streams on at least one supported Raspberry
  Pi and one amd64 Linux host.

**Documentation/commit:** update all docs to describe shipped behavior, mark
Step 10 Done, create a changelog entry, and commit as
`chore: prepare initial release`.

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
