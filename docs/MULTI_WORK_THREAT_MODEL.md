# Multi-work mapping threat model

This note supplements [`SECURITY.md`](SECURITY.md) for the feature contract in
[`PLAN.md`](../PLAN.md). Media remains trusted personal content operationally,
but filenames, sidecars, provider data, and model output are treated as
untrusted inputs.

## Assets and trust boundaries

- MP4 files and their paths must remain read-only and playable regardless of
  mapping success.
- SQLite mappings and the last published site must survive malformed input,
  network failure, and interrupted scans.
- The OpenAI API key and protected INI must not enter prompts, logs, generated
  pages, fixtures, or provider requests.
- Tenrai verifies MAL records; the LLM is an inference aid and is not an
  identity authority.

## Threats and controls

| Threat | Required control |
|---|---|
| Path traversal or malicious basenames | Exact overrides operate on basenames only; reject NUL, separators, absolute paths, `.`/`..`, control characters, and missing files |
| Expensive or surprising pattern matching | Accept case-insensitive standard-library globs only; no regex; bound rules, patterns, lengths, and comparisons to the current basename set |
| Conflicting manual rules | Exact override has priority; two matching work rules produce a bounded scan error and leave the last known-good mapping intact |
| Candidate explosion | At most 12 works, relation depth 3, cycle detection by provider/ID, and existing provider throttling/retry budgets |
| Remote relation cycles or poisoned fields | Follow only reviewed anime relation types; validate every ID, type, title, count, URL, and payload size before persistence or rendering |
| Model-proposed or hallucinated IDs | Send a bounded verified candidate set; schema permits only those request-specific IDs or `null`; reject all other IDs application-side |
| Model remaps manual decisions | Exclude exact mappings from unresolved input and enforce precedence again after output validation |
| Prompt or log injection through filenames | Send filenames as structured data under fixed instructions; cap lengths/counts; use strict output; sanitize logs and HTML-escape rendered text |
| Paid-call amplification | Deterministic/manual mapping first, per-scan call budget, digest cache, transient cooldown, and no automatic cache bypass on `SIGHUP` |
| Stale mappings after rename or rule change | Digest filename facts, candidate versions, parser/schema/model, and canonical rule input; invalidate only affected lower-precedence mappings |
| Partial provider/model outage | Use normalized cache where valid, retain pending manual declarations, leave uncertain files unmapped, and preserve the last published site |

## Frozen input bounds

The implementation must enforce these initial limits:

| Input | Limit |
|---|---:|
| Work sections per collection | 12 |
| Exact media sections per collection | 50 |
| Total sidecar sections | 64 |
| Related/manual candidate MAL IDs | 12 |
| Glob patterns per work | 8 |
| Characters per glob | 256 |
| Total glob characters per collection | 2,048 |
| Work/media section name | 64 characters |
| Display/group/media label | 120 characters |
| Basename accepted for mapping/inference | 300 characters |
| Episode or episode-range endpoint | 1–9,999 |
| Absolute episode offset | 9,999 |
| Group order | 0–10,000 |
| Relation traversal depth | 3 |
| Verified candidate works | 12 |
| Filenames per model request | 50 |

Limits may change only with tests, documentation, and an ADR update when they
materially alter the security or operability boundary.
