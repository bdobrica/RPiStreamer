# ADR 0003: MAL identity with Tenrai as the default transport

- Status: Accepted
- Date: 2026-07-25

## Context

Jikan v4 supplied the initial MyAnimeList metadata but produced persistent and
intermittent HTTP 504 failures for valid entries. Tenrai v1 is an authless,
Jikan-compatible continuation using the same MAL identifiers.

## Decision

Tenrai v1 is the default metadata transport. Jikan v4 remains an explicit
rollback transport and `none` provides offline operation. Both transports use
the same validated client and the legacy logical SQLite provider key `jikan`,
which represents the Jikan-compatible MAL schema rather than a particular
host.

Existing `mal_id` pins, normalized records, relations, episodes, artwork, and
page identity survive a transport switch. Validator provenance is stored
separately so an `ETag` or `Last-Modified` value from one host is never sent to
the other. Base URLs are trusted compiled profiles rather than arbitrary
configuration.

## Consequences

New native and container configurations use Tenrai without duplicating cached
catalogue data. Existing INI files remain operator-owned and must be changed
explicitly during upgrade. A future provider with a different schema or
identity namespace requires a new ADR and migration strategy.
