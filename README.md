# RPi Streamer

RPi Streamer is a small, local-network media catalogue for personal MP4
collections. Nginx serves the media files with HTTP byte-range support so a
browser can seek and stream without downloading an entire file first. A
periodic Python indexer scans the library, stores its catalogue in SQLite,
enriches anime folders with metadata, and generates static HTML pages for
Nginx to serve.

The project is intended to run comfortably on a Raspberry Pi. It does not
transcode video, manage users, or expose a public internet service.

> **Project status:** design and implementation plan only. No runnable service
> has been implemented yet. See [PLAN.md](PLAN.md) for the tracked roadmap.

## Goals

- Stream existing `.mp4` files through Nginx, including browser seeking.
- Browse folders, series, related titles, genres, and locally available
  episodes from generated pages.
- Detect additions, changes, moves, and removals during periodic scans.
- Cache catalogue and metadata state in SQLite.
- Fetch anime details, cover art, episode information, genres, and
  prequel/sequel relationships without requiring a MyAnimeList login.
- Run as a native systemd service or in containers.
- Configure the same application with an INI file and environment variables.
- Continue serving the last successful catalogue when scanning or metadata
  lookup fails.

## Non-goals

- Video transcoding, remuxing, or adaptive-bitrate streaming.
- Authentication, authorization, or safe exposure to the public internet.
- Editing a MyAnimeList account or watch history.
- Downloading copyrighted media.
- A heavy, always-running application web framework.

MP4 browser compatibility still depends on the codecs in each file. Nginx can
serve any MP4, but common browser-compatible combinations such as H.264 video
and AAC audio provide the broadest playback support.

## Proposed architecture

```text
                        metadata requests
                     ┌─────────────────────┐
                     │ Jikan REST API v4  │
                     └──────────▲──────────┘
                                │ cached/rate-limited
┌────────────────┐    scan      │     ┌─────────────────────┐
│ media mount    │───────────────┼────▶│ Python indexer      │
│ /mnt/anime     │               │     │ + static renderer   │
└───────┬────────┘               │     └──────┬────────┬─────┘
        │                        │            │        │
        │ MP4 files              │      SQLite│        │ atomic HTML/images
        │                        │            ▼        ▼
        │                        │       /var/lib/rpi-streamer/
        ▼                        │       ├── catalogue.db
┌────────────────────────────────────────────┴───────────────┐
│ Nginx                                                      │
│ /media/... -> configured media mount                       │
│ /          -> generated static catalogue                   │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
                           web browser
```

Nginx is the data plane: it handles large files, MIME types, conditional
requests, and byte ranges efficiently. Python is the control plane: it scans
and generates pages, but is not in the video path. Static generation is
preferred over FastAPI because the catalogue changes infrequently and requires
no authentication or per-user state. A dynamic API can be added later without
changing media URLs.

The first implementation will use the public, read-only
[Jikan REST API v4](https://docs.api.jikan.moe/) as the default metadata
provider. Jikan is an unofficial MyAnimeList API, supports conditional
requests, and currently documents limits of 3 requests/second and 60
requests/minute. RPi Streamer will operate below those limits, persist fetched
responses, honor `ETag`/`Last-Modified`, retry transient failures with backoff,
and never make metadata availability a requirement for local playback.

## Expected library layout

The scanner treats each directory containing MP4 files as a title and sorts
media using natural filename order.

```text
/mnt/anime/
├── Cowboy Bebop/
│   ├── 01 - Asteroid Blues.mp4
│   ├── 02 - Stray Dog Strut.mp4
│   └── rpi-streamer.ini        # optional per-title overrides
└── Neon Genesis Evangelion/
    ├── S01E01.mp4
    └── S01E02.mp4
```

Folder names are used as search hints, not unquestioned identities. Matching
must be deterministic and reviewable. An optional per-title sidecar will allow
the owner to pin a MyAnimeList ID, display title, sort title, or disable remote
metadata. Files and directories outside the configured mount are never
catalogued. Symlinks that resolve outside it will be rejected by default.

## Generated catalogue

The initial UI will be server-rendered static HTML with no JavaScript
requirement:

- a home page with title cards, cover images, and scan status;
- a folder/title page with metadata and locally available MP4 episodes;
- genre pages and links between known prequels and sequels;
- breadcrumbs and a simple title filter;
- an HTML5 `<video controls preload="metadata">` player;
- graceful placeholders when metadata or artwork is unavailable.

All user-controlled filenames and remote text are HTML-escaped. Media links
are URL-encoded and rooted below `/media/`. A catalogue build is written to a
staging directory and swapped into place only after it completes, preventing
Nginx from serving a partially generated site.

## Configuration model

Native installations read `/etc/rpi-streamer/rpi-streamer.ini`. A different
file can be selected with `RPI_STREAMER_CONFIG`. Environment variables override
INI values, which makes the same image usable in a container.

The intended initial schema is:

```ini
[rpi-streamer]
media_root = /mnt/anime
state_dir = /var/lib/rpi-streamer
site_dir = /var/lib/rpi-streamer/site
database_path = /var/lib/rpi-streamer/catalogue.db
scan_interval = 1h
metadata_provider = jikan
metadata_refresh_interval = 30d
metadata_language = en
download_artwork = true
log_level = INFO
```

| INI key | Environment override | Purpose |
|---|---|---|
| `media_root` | `RPI_STREAMER_MEDIA_ROOT` | Read-only root containing the collection |
| `state_dir` | `RPI_STREAMER_STATE_DIR` | Persistent application state |
| `site_dir` | `RPI_STREAMER_SITE_DIR` | Generated pages and cached artwork |
| `database_path` | `RPI_STREAMER_DATABASE_PATH` | SQLite database file |
| `scan_interval` | `RPI_STREAMER_SCAN_INTERVAL` | Delay between automatic scans; `0` disables them |
| `metadata_provider` | `RPI_STREAMER_METADATA_PROVIDER` | `jikan` or `none` initially |
| `metadata_refresh_interval` | `RPI_STREAMER_METADATA_REFRESH_INTERVAL` | Maximum metadata cache age |
| `metadata_language` | `RPI_STREAMER_METADATA_LANGUAGE` | Preferred display-title language |
| `download_artwork` | `RPI_STREAMER_DOWNLOAD_ARTWORK` | Cache covers locally |
| `log_level` | `RPI_STREAMER_LOG_LEVEL` | Application log verbosity |

Durations will accept documented suffixes such as `s`, `m`, `h`, and `d`.
Startup validation will reject missing roots, relative paths, invalid values,
and conflicting state/site paths. Secrets are not required by the default
provider.

## Process lifecycle

The indexer performs a scan at startup and then waits for the configured
interval:

- `SIGHUP` requests an immediate rescan (coalesced if one is already running);
- `SIGTERM` and `SIGINT` request a graceful shutdown;
- a failed scan is logged and retried later while the previous generated site
  remains available.

The CLI is expected to provide foreground service operation plus one-shot
commands useful for testing and administration:

```text
rpi-streamer serve
rpi-streamer scan
rpi-streamer validate-config
```

For systemd, `systemctl reload rpi-streamer` will send `SIGHUP`. Scans will also
be triggerable with `kill -HUP "$(pidof rpi-streamer)"` where appropriate.

## Native deployment target

Packaging will install:

```text
/etc/rpi-streamer/rpi-streamer.ini
/etc/nginx/sites-available/rpi-streamer.conf
/etc/systemd/system/rpi-streamer.service
/var/lib/rpi-streamer/
```

The service will use a dedicated unprivileged account, a writable state
directory, a read-only media mount, systemd hardening, and journald logging.
Nginx will receive read/traverse permission for the media tree and read
permission for the generated site. Its configuration will bind to a
configurable LAN address/port and expose `/media/` through `alias`.

This is intentionally a trusted-LAN design. Operators should use a firewall and
must not port-forward it to the internet without adding authentication, TLS,
request limits, and a separate security review.

## Container deployment target

The planned Compose setup uses two small services:

- `indexer`: the Python application with the media volume mounted read-only
  and state/site volume mounted read-write;
- `nginx`: the generated site and media volumes mounted read-only.

SQLite and generated output live in a persistent volume. Configuration is
provided through `RPI_STREAMER_*` variables. Containers share no Docker socket
and run without privileged mode. Multi-architecture images will target at
least `linux/amd64` and `linux/arm64`.

## Data and rescan behavior

SQLite will track discovered titles and media files, provider identifiers and
raw/normalized metadata, relationships, genres, artwork, scan runs, and cache
validators. Files will be identified by normalized path plus inexpensive
stat information; the initial version will not hash multi-gigabyte videos.

A successful full scan marks missing entries unavailable rather than
immediately destroying history. Remote calls happen only for new, manually
rematched, or stale titles. Database migrations are versioned and transactional.
SQLite uses foreign keys, a busy timeout, and WAL mode where the deployment
filesystem supports it.

## Development

The concrete package layout, quality gates, fixtures, deployment assets, and
acceptance tests are specified in [PLAN.md](PLAN.md). The project will follow
this workflow for every implementation milestone:

1. implement one tracked step and its tests;
2. run the checks appropriate to that step;
3. update this README with behavior that is now real;
4. update the status and notes in `PLAN.md`;
5. commit the cohesive change with a descriptive message.

Until a milestone is marked **Done**, its interface in this README is a design
target and may change during implementation.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
