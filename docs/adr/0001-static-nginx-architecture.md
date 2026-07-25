# ADR 0001: Static catalogue with Nginx as the data plane

- Status: Accepted
- Date: 2026-07-25

## Context

The service runs on a Raspberry Pi, changes only when the local collection is
rescanned, and does not need authentication or per-user state. Python is not a
good place to proxy large MP4 bodies when Nginx already provides efficient
HTTP byte ranges, MIME handling, and conditional requests.

## Decision

Nginx serves generated HTML, local artwork, and MP4 files directly. The Python
process is a control plane that scans, enriches, persists, and atomically
generates static pages. Title pages use one dependency-free JavaScript player
controller for episode selection. FastAPI, HLS, and transcoding are deferred
until a concrete requirement justifies their runtime cost.

## Consequences

Playback and seeking remain available when the Python process is idle or a
metadata provider is offline. The generated site is disposable and can be
rebuilt from SQLite. Dynamic search, user accounts, and playback-state APIs are
not provided.
