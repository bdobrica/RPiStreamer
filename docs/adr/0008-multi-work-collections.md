# ADR 0008: Multi-work collections with verified file mappings

- Status: Accepted
- Date: 2026-07-26

## Context

A local folder can contain several seasons or related media while the current
catalogue associates it with one MAL record. This flattens cumulative episode
numbers, omits later provider episode context, and cannot distinguish movies,
OVAs, summaries, or specials. Real examples include a 37-file collection
numbered continuously across three seasons and a two-season collection whose
second-season filenames reset to episode 1.

Folder names and filenames provide useful evidence but do not safely identify
every tie-in. Model assistance can help with ambiguous release names, but it
must not invent or verify metadata identities.

## Decision

A filesystem title folder is a local collection with one primary work and zero
or more related verified MAL works. Each media file may map to at most one
associated work plus an optional episode range or media kind. Unmapped files
remain visible and playable.

The existing sidecar gains:

- `[work "NAME"]` rules using verified MAL IDs and bounded basename globs,
  parsed seasons, or local episode ranges with offsets;
- `[media "NAME"]` exact-basename overrides for exceptions and tie-ins;
- optional `related_mal_ids` candidate declarations.

Selectors in one work rule are combined with logical AND. Exact manual
mappings override manual work rules, which override deterministic mapping,
which overrides accepted cached or new model mapping. Multiple matching manual
work rules are an error. A selectorless primary work is only a fallback for
otherwise safely single-work media; it does not override explicit or
ambiguous evidence.

All MAL IDs are verified through the configured Jikan-compatible transport or
an existing normalized cache. Relation discovery is cycle-safe and bounded by
depth and candidate count. The model receives only verified candidates and
must choose one of those IDs or `null` through strict Structured Outputs.
Application validation still enforces filenames, candidate membership,
episode bounds, confidence, and precedence.

The static title page keeps its stable collection URL, primary header and
cover, and one video player. The episode selector groups mapped files by the
preferred provider title in the configured metadata language. Provider
episode context is rendered per work, and uncertain files appear in an
`Unmapped` group.

## Consequences

Existing `mal_id` sidecars and single-work pages remain compatible. Common
continuous and reset numbering layouts can be mapped without model calls.
Operators can correct any result locally and corrections take effect at the
next scan.

The persistence model must normalize provider records away from a one-folder
ownership assumption and add collection-work associations and per-file mapping
provenance. Relation and inference inputs require bounded caches and
invalidation. Grouped rendering and migration add complexity, but local
playback remains independent of provider or model availability.

Schema version 6 implements that persistence boundary. `provider_records` are
global to the provider/MAL identity; `library_entry_works` owns collection
association and verification provenance; and `media_work_mappings` owns the
optional file classification and mapping provenance. A canonical rules digest
is stored separately for future invalidation. The schema-5 migration preserves
provider record IDs and their complete metadata graph, creates one primary
association per existing match, and intentionally leaves files unmapped for
the first reconciliation.

Deleting a collection removes only its associations and mappings. It does not
delete a normalized provider record still referenced by another collection.
The migration is one transaction and performs a foreign-key integrity check
before recording completion.

Schema version 7 and the sidecar reconciliation layer implement the manual
control boundary. Parsing is interpolation-free and bounded by the threat
model. Exact filenames are case-sensitive basenames; work globs are
case-insensitive, basename-only, and compared only with the collection’s
discovered files. A canonical rules digest stabilizes unchanged mappings and
limits invalidation to derived rows.

Manual IDs become associations only after normalized-cache or live
Jikan-compatible anime verification. Uncached IDs remain pending during an
outage. Exact overrides precede AND-combined work selectors, and overlapping
work rules retain the last valid result while making the scan partial.

Schema version 8 and the candidate discovery layer implement the verified
relation boundary. Expansion begins at the primary work only when local,
manual, or cached-relation evidence suggests multiple works. It follows the
reviewed anime relation vocabulary, prioritizes sequels, detects cycles, and
stops at depth 3 or 12 total collection works. Normalized provider records are
the offline cache; missing targets use the shared provider verifier and its
throttling. Relation associations persist source and distance, while partial
outages preserve the last verified set. No model participates in candidate
identity discovery.

Schema version 9 and the deterministic mapping layer implement the automatic
file boundary. Filename facts are parsed separately from presentation hints.
Cumulative mappings require a complete contiguous sequence whose endpoint
uniquely matches verified ordered episode counts; reset numbering requires an
explicit later-season marker. Provider bounds and cross-boundary ranges are
rejected, while incomplete, duplicate, or non-unique layouts remain unmapped
or ambiguous. Manual mappings retain precedence. Each deterministic row stores
a mapping-schema version and an input digest covering filename facts, ordered
verified candidates/counts, parser version, and canonical sidecar rules, so
renames and metadata changes invalidate only affected derived rows.

Arbitrary regular expressions, unverified model IDs, unbounded franchise
graphs, and silent conflict resolution are deliberately excluded.

The grouped renderer implements the presentation boundary without changing
collection slugs or media URLs. Associated works are ordered by their persisted
manual/relation display order, then stable provider identity. One native
`<select>` contains a work `<optgroup>` for each mapped set and a final
`Unmapped` group. Its flat option order drives Previous/Next navigation across
boundaries and preserves the existing `#episode-N` deep links. The selected
work and cached provider episode title are updated next to the sole video
player. Direct, grouped `<noscript>` links keep every file playable without
JavaScript, while compact cards and per-work episode tables expose the
additional cached metadata.

Related works reuse the primary metadata path's bounded artwork policy.
Newly verified works cache their cover immediately; previously normalized
related records without an artwork row are backfilled from the stored provider
payload on a metadata-enabled scan. Artwork remains optional and a download
failure never blocks association, mapping, generation, or playback.

Operators can inspect one exact collection through deterministic, bounded JSON
without exposing raw sidecars, cache digests, prompts, responses, or secrets.
Sidecar validation is read-only. Candidate refresh, model-only invalidation,
and deterministic recomputation share the service instance lock and affect
only the selected collection. Ordinary `SIGHUP` rescans retain valid provider
and model caches and do not bypass the configured inference budget. Scan logs
contain aggregate mapping and external-call counters rather than uncontrolled
filename lists.

The offline acceptance boundary runs both reported layout classes together:
37 continuously numbered files across three works and 37 reset-numbered files
across two works. It exercises pinned provider fixtures, verified candidates,
manual mapping, SQLite persistence, grouped and no-JavaScript generation,
cache-only rescans, rename identity, and removal reconciliation. Hardware
playback, seeking, byte ranges, resource measurements, and real provider/model
call counts remain host acceptance evidence and are recorded with the
sanitized runbook rather than inferred from unit tests.

Acceptance requires manual exact and range corrections to win on the next
scan, uncertain files to remain visible and playable, existing single-title
URLs to remain stable, and cached/manual operation to survive Tenrai or OpenAI
being disabled independently. The automated suite also preserves schema-5
upgrade/rollback, malformed-sidecar, partial-provider, model-failure, atomic
generation, native/Compose configuration, and Nginx range boundaries. The
[host acceptance runbook](../MULTI_WORK_ACCEPTANCE.md) is the authoritative
place for measured Raspberry Pi evidence.
