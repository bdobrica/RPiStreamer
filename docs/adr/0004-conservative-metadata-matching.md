# ADR 0004: Conservative and explainable metadata matching

- Status: Accepted
- Date: 2026-07-25

## Context

Folder names are useful search hints but are not reliable identities. A false
positive silently attaches the wrong synopsis, artwork, relations, and episode
context to personal media.

## Decision

Matching normalizes Unicode, case, punctuation, and whitespace, then scores
canonical and alias titles. A candidate must meet both a confidence threshold
and an ambiguity margin. One bounded final-word retry handles truncated long
romanized titles without globally lowering the threshold. Low-confidence or
ambiguous titles remain visibly unmatched.

A per-title `mal_id` sidecar pin is authoritative, and
`metadata_enabled = false` disables enrichment for that title. Match outcomes
are logged with bounded context. Local filenames and media URLs remain
authoritative even when provider episode information exists.

## Consequences

Some titles require an explicit pin, but incorrect automatic matches are less
likely. Matching remains deterministic, testable offline, and explainable to
an operator.
