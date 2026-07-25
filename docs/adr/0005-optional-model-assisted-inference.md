# ADR 0005: Optional, bounded model-assisted inference

- Status: Accepted
- Date: 2026-07-25

## Context

Release filenames and romanized folder names sometimes defeat deterministic
parsing. Model assistance can normalize these inputs, but it introduces
credentials, cost, privacy, nondeterminism, and an additional network failure
mode.

## Decision

OpenAI inference is disabled by default. When enabled, the Responses API uses
strict, versioned structured output, fixed input and output limits, a per-scan
call budget, a timeout, and SQLite caching. Only the relative title name and
bounded MP4 basenames are sent; no media content, absolute paths, sidecars,
database records, provider metadata, or API key enters the prompt.

Deterministic parsing and MAL pins remain authoritative. An inferred title is
only another metadata search query and must pass the normal candidate checks.
Episode hints are stored separately with confidence and never replace the
original filename or URL. Model failures are non-fatal. The API key may be kept
in the protected native INI or supplied through the environment and is always
redacted from diagnostics.

## Consequences

Unusual naming can be improved with bounded cost while disabled installations
retain the standard-library runtime footprint. Verified metadata still depends
on the configured MAL transport, and operators must protect configuration
backups containing a key.
