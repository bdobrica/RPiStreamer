# ADR 0002: SQLite catalogue and read-only media scanning

- Status: Accepted
- Date: 2026-07-25

## Context

The original media collection must not be modified, while rescans must retain
metadata through renames, temporary mount failures, and partial filesystem
visibility. Video hashing would make routine scans unnecessarily expensive.

## Decision

SQLite is the source of catalogue state and uses ordered, transactional,
forward-only migrations. The scanner reads MP4 files and optional per-title
sidecars without writing under `media_root`. It records canonical relative
paths, size, nanosecond modification time, and filesystem identity; video
content is not hashed. Missing items become unavailable rather than being
deleted. A partial scan updates known discoveries but does not mark unseen
subtrees unavailable.

SQLite uses foreign keys, a busy timeout, and WAL where the filesystem supports
it. Generated pages and downloaded artwork use atomic replacement.

## Consequences

Unambiguous moves on the same filesystem retain catalogue identity and cached
metadata. Scans stay proportional to directory and stat operations rather than
media size. Operators must back up SQLite consistently, including WAL state or
using SQLite's backup API, and older binaries cannot open newer schemas.

Multi-work operator mutations use the same instance lock as scans and are
scoped by collection foreign keys. Model invalidation deletes only selected
model mapping rows and their exact inference-cache keys; provider records and
media rows remain intact. Sidecar validation and mapping inspection are
read-only.
