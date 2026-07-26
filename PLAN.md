# Multi-work folder mapping implementation plan

This plan adds support for media folders that contain several MyAnimeList
works: multiple seasons, movies, OVAs, summaries, specials, and other tie-ins.
It is intentionally feature-specific. The completed project architecture
remains documented in [`docs/adr/`](docs/adr/README.md).

Every increment must preserve existing single-title folders, local playback,
the last published catalogue, and offline operation. Each completed increment
ends with tests, README and ADR/changelog updates where relevant, and one
focused commit.

Status values are **Pending**, **In progress**, **Blocked**, and **Done**.

## Status

| Step | Increment | Status | Completion evidence |
|---:|---|---|---|
| 0 | Contract, fixtures, and architecture decision | Done | Frozen contract, ADR 0008, threat model, three sanitized fixture families, and 118 tests pass |
| 1 | Multi-work SQLite model and migration | Done | Schema 6 migration preserves provider graphs/IDs, adds constrained associations and mappings, and 122 tests pass |
| 2 | Sidecar work rules and exact media overrides | Done | Bounded parser, cached/live verification, digest reconciliation, manual precedence, and 135 tests pass |
| 3 | Tenrai relation discovery and candidate cache | Done | Depth-3/12-work verified graph, suspicion gate, offline cache, provenance, and 143 tests pass |
| 4 | Deterministic file-to-work mapping | Pending | MF Ghost and Tsukimichi map without model calls where rules or filenames suffice |
| 5 | Structured LLM-assisted mapping | Pending | Unresolved files receive validated, cached candidate mappings within the call budget |
| 6 | Grouped catalogue rendering | Pending | One player groups seasons and tie-ins while retaining no-JS playback |
| 7 | Operator controls, observability, and documentation | Pending | Mapping inspection/refresh commands and complete README guidance are available |
| 8 | End-to-end and Raspberry Pi acceptance | Pending | Upgrade, cold/cached scans, manual correction, playback, and resource evidence pass |

## Goals

- Allow one local folder to contain files belonging to multiple verified MAL
  anime records.
- Map each file to a work plus an episode, range, movie, OVA, OAD, ONA,
  special, summary, or unknown classification.
- Resolve common multi-season layouts deterministically before using the LLM.
- Use the LLM only for unresolved or ambiguous mappings and restrict it to
  Tenrai-verified candidate MAL IDs.
- Let operators declare compact work rules and exact file overrides in the
  existing per-folder `rpi-streamer.ini`.
- Group the existing single-player selector by season or tie-in work without
  changing media URLs or Nginx streaming.
- Keep uncertain files playable and clearly marked instead of silently
  assigning incorrect metadata.

## Non-goals

- Moving, renaming, reorganizing, hashing, or modifying media files.
- Treating an LLM-produced MAL ID as verified metadata.
- Searching an unbounded franchise graph or downloading all related works.
- Inferring disc order, cuts, multipart movies, or arbitrary fansub numbering
  perfectly.
- Replacing the static catalogue with a dynamic application.
- Requiring OpenAI for multi-work folders or ordinary scans.
- Supporting provider identity namespaces other than the existing
  Jikan-compatible MAL schema in this increment.

## Decisions fixed by this plan

1. A filesystem title folder becomes a **local collection**. It retains its
   current display title, slug, page URL, media paths, and primary work.
2. A **work** is one verified MAL anime record associated with a collection.
   The current `mal_id` remains the primary-work pin.
3. A **media mapping** associates one local file with at most one work and an
   optional episode/special classification. The file remains playable without
   a mapping.
4. Candidate MAL IDs originate only from a manual sidecar value, the current
   verified primary match, deterministic Tenrai search, or a bounded Tenrai
   relation graph. The LLM cannot expand that set.
5. Mapping precedence is:
   exact manual media override, manual work rule, deterministic filename/work
   mapping, accepted cached model mapping, new model mapping, then unmapped.
6. A lower-precedence source never overwrites a valid higher-precedence
   mapping. Two matching manual work rules are a scan error, not an arbitrary
   tie-break.
7. Manual mappings are allowed even when OpenAI inference is disabled.
8. Existing `[rpi-streamer] mal_id = ...` sidecars and folders with no new
   sections retain their current behavior and generated URLs.
9. Glob matching is case-insensitive and limited to basenames. Arbitrary
   regular expressions are not accepted.
10. Structured Outputs constrain the model response, and application-side
    validation still enforces exact filenames, candidate membership,
    episode bounds, confidence, and precedence. See the official
    [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs#structured-outputs-vs-json-mode).

## Frozen sidecar contract

The existing section remains valid:

```ini
[rpi-streamer]
display_title = MF Ghost
sort_title = MF Ghost
metadata_enabled = true
mal_id = 50695
```

`mal_id` identifies the primary work. Optional `related_mal_ids` adds verified
candidate works that relation discovery cannot find or that the operator wants
to allow explicitly:

```ini
[rpi-streamer]
mal_id = 50695
related_mal_ids = 12345, 67890
```

The example IDs above are placeholders except for the supplied primary
`50695`; documentation and tests must never present unverified IDs as real.

### Work rules

Each `[work "NAME"]` section has an operator-local unique name and one required
positive `mal_id`. Optional selectors are combined with logical AND. At least
one selector is required unless the rule represents the primary work:

```ini
[work "season-2"]
mal_id = 12345
label = MF Ghost 2nd Season
local_episode_range = 13-24
episode_offset = -12
order = 20
```

Supported keys:

| Key | Meaning |
|---|---|
| `mal_id` | Required verified MAL anime ID |
| `label` | Optional local group label; provider title is the default |
| `files` | Optional multiline, case-insensitive basename globs |
| `season` | Optional positive parsed local season number |
| `local_episode_range` | Optional inclusive parsed local episode range |
| `episode_offset` | Signed integer added after matching, default `0` |
| `kind` | Optional forced `episode`, `movie`, `ova`, `oad`, `ona`, `special`, `summary`, or `unknown` |
| `order` | Optional non-negative group order |

Examples for the supplied layouts:

```ini
[rpi-streamer]
display_title = MF Ghost
mal_id = 50695

[work "season-1"]
mal_id = 50695
local_episode_range = 1-12
episode_offset = 0
order = 10

[work "season-2"]
mal_id = 12345
local_episode_range = 13-24
episode_offset = -12
order = 20

[work "season-3"]
mal_id = 67890
local_episode_range = 25-37
episode_offset = -24
order = 30
```

```ini
[rpi-streamer]
display_title = Tsuki ga Michibiku Isekai Douchuu
mal_id = 43523

[work "season-1"]
mal_id = 43523
season = 1
order = 10

[work "season-2"]
mal_id = 12345
season = 2
files =
    *2nd_Season*
order = 20
```

The placeholder related IDs must be replaced with verified Tenrai/MAL IDs by
the operator or automatic candidate discovery.

### Exact media overrides

An exact override uses a stable local section name and an exact basename. This
avoids putting arbitrary filename characters into an INI section name:

```ini
[media "battle-digest"]
file = AnimePahe_MF_Ghost_Battle_Digest.mp4
mal_id = 54321
kind = summary
label = Battle Digest

[media "special-1"]
file = AnimePahe_MF_Ghost_Special_01.mp4
mal_id = 98765
kind = special
episode = 1
```

Supported keys:

| Key | Meaning |
|---|---|
| `file` | Required exact basename of a local MP4 |
| `mal_id` | Required positive verified candidate ID |
| `episode` | Optional positive episode number |
| `episode_end` | Optional positive inclusive range end |
| `kind` | Optional classification from the work-rule vocabulary |
| `label` | Optional display label |

Unknown sections or keys, duplicate rule names, duplicate exact filenames,
missing files, invalid globs, impossible ranges, non-anime IDs, or conflicting
manual rules are reported as bounded scan errors. A malformed sidecar must not
remove previously published media or rewrite the media directory.

### Frozen matching semantics and bounds

- Section names must match `[rpi-streamer]`, `[work "NAME"]`, or
  `[media "NAME"]`. Work/media names are unique, case-sensitive identifiers of
  1–64 printable characters without quotes or control characters.
- `files` contains at most eight case-insensitive basename globs per work.
  Globs use standard `*`, `?`, and character-class behavior; path separators,
  absolute paths, NULs, and `..` path components are invalid.
- When a work supplies multiple selectors (`files`, `season`, and
  `local_episode_range`), all supplied selectors must match.
- A selectorless rule is permitted only for the primary `mal_id` and acts as a
  safe single-work fallback after exact rules and ambiguity checks; it is not a
  catch-all override.
- `local_episode_range` compares against the conservatively parsed local
  episode start. `episode_offset` is applied only after a work rule matches and
  must yield episode endpoints from 1 through 9,999.
- `kind` is an output classification, not a selector. `episode_end` requires
  `episode` and cannot be less than it.
- Exact `file` matching is case-sensitive because it names a concrete local
  basename. Glob matching is case-insensitive for portability with the
  scanner's extension behavior.
- Manual MAL IDs add candidates but become authoritative only after a cached or
  live provider record verifies them as anime. Offline unverified declarations
  remain pending.
- Group labels use each work's preferred provider title under
  `metadata_language`; a manual `label` overrides it. The collection header,
  synopsis, genres, and cover remain those of the primary work initially.
- Hard limits are: 12 work sections, 50 exact media sections, 64 total
  sections, 12 manual/related candidates, 256 characters per glob, 2,048 total
  glob characters, 120-character labels, 300-character basenames, episode and
  absolute offset bounds of 9,999, order 0–10,000, relation depth 3, 12
  verified candidate works, and 50 filenames per model request. The
  authoritative table and rationale are in
  [`docs/MULTI_WORK_THREAT_MODEL.md`](docs/MULTI_WORK_THREAT_MODEL.md).

### Frozen conflict and failure outcomes

| Condition | Outcome |
|---|---|
| Exact media override and work rule both match | Exact override wins |
| Two work rules match one file | Scan issue; retain last valid mapping or leave unmapped |
| Manual ID is invalid/non-anime | Scan issue; rule is not applied |
| Manual ID is unverified while offline | Declaration remains pending; no new mapping |
| Deterministic result conflicts with manual mapping | Manual mapping wins |
| Model result conflicts with manual/deterministic mapping | Higher-precedence mapping wins |
| Model returns unknown filename or candidate ID | Reject the affected response as invalid |
| Episode is outside a known provider count | Reject mapping unless an explicitly allowed non-episode kind applies |
| Filename is missing after a sidecar edit | Scan issue; no mapping row is created |
| Sidecar exceeds a frozen bound | Reject the sidecar extension and preserve last-known-good published state |

## Step 0 — Contract, fixtures, and ADR

**Status: Done**

- Add sanitized synthetic fixtures representing the supplied failure modes:
  - MF Ghost: 37 files numbered continuously, primary work with 12 provider
    episodes, and a sequel chain covering three seasons.
  - Tsukimichi: 12 first-season files plus 25 `2nd Season` files whose episode
    numbers reset, with the second work available as a sequel.
  - One folder containing a movie, OVA, summary, ambiguous basename, and an
    unrelated numeric filename.
- Use synthetic filenames patterned after the examples; do not commit the
  supplied generated HTML, remote response bodies, or personal paths.
- Finalize names, types, bounds, matching semantics, precedence, and failure
  behavior for every sidecar key above.
- Decide whether group labels follow configured metadata language and how a
  primary cover is selected. Initial decision: use each work's preferred
  provider title for its group and retain the primary work's cover/header.
- Add ADR 0008 describing multi-work collections, verified candidate
  boundaries, manual precedence, and grouped rendering.
- Add a small threat-model note covering glob complexity, malicious filenames,
  model-proposed IDs, candidate explosion, and remote relation cycles.

**Acceptance**

- Each supplied real-world layout is expressible without one sidecar section
  per ordinary episode.
- Existing sidecar examples remain valid without edits.
- Ambiguous precedence and invalid configurations have explicit outcomes.

**Commit:** `docs: define multi-work collection mapping`.

**Delivered:** the sidecar grammar, selector semantics, precedence, labels,
primary presentation, bounds, and failure matrix are frozen above; ADR 0008
records the architectural decision; the dedicated threat model covers glob,
filename, provider-graph, model-ID, prompt/log, cost, invalidation, and outage
risks; and three sanitized fixture families cover 37-file cumulative
numbering, 12+25 reset numbering, and movie/OVA/summary/ambiguous/numeric
tie-ins. Fixture IDs are deliberately synthetic, compact JSON expands to the
required file counts, example sidecars use at most four sections, and no
personal path, copied HTML, provider response, or media content is stored.
Ruff, formatting, strict mypy, and 118 offline tests pass; five
environment-dependent tests skip.

## Step 1 — Multi-work SQLite model and migration

**Status: Done**

Normalize the current one-provider-record-per-folder relationship before
introducing mapping behavior.

- Add a forward-only schema migration from version 5.
- Make normalized provider records reusable independently of a library entry,
  keyed by the existing logical provider namespace and provider ID.
- Add `library_entry_works` with:
  - library entry and provider record foreign keys;
  - primary flag;
  - local work name and optional label;
  - deterministic display order;
  - association source (`matched`, `manual`, `relation`, or `model`);
  - confidence where applicable;
  - first/last verification timestamps.
- Add `media_work_mappings` with:
  - media file and associated work foreign keys;
  - kind, episode start/end, and optional display label;
  - source (`manual_exact`, `manual_rule`, `deterministic`, or `model`);
  - confidence and inference schema/model where applicable;
  - mapping input digest and timestamps.
- Add a normalized table for manual work rules or store a canonical rules
  digest sufficient to invalidate derived mappings. Do not store secrets.
- Preserve provider episodes, aliases, genres, relations, artwork, raw
  diagnostics, transport validator provenance, inference cache, and existing
  record IDs where safely possible.
- Convert every existing provider match into one primary
  `library_entry_works` row. Existing media files remain unmapped until the
  first reconciliation, then inherit the primary work when safe.
- Define cascades so deleting an unavailable file removes only its mapping;
  removing a work association never deletes a provider record still used by
  another collection.
- Keep the migration transactional. If SQLite table rebuilding is required,
  test foreign-key integrity before commit and retain normal backup guidance.

**Tests**

- Fresh schema creation, v5 migration, migration rollback, idempotent reopen,
  foreign keys, uniqueness, cascades, and future-schema rejection.
- A migrated single-title database renders the same slug, metadata, artwork,
  episodes, relations, and player ordering as before.
- The same MAL work may be associated with more than one local collection
  without duplicating normalized provider metadata.
- Mapping provenance and confidence constraints reject invalid values.

**Documentation/commit:** update the README data-model table and backup warning;
commit as `feat: add multi-work catalogue schema`.

**Completion:** schema version 6 rebuilds provider metadata as reusable
provider/ID records, migrates every schema-5 match to a primary association,
and adds constrained collection-work, file-mapping, and rules-digest storage.
Compatibility queries keep existing single-title rendering intact. Migration,
rollback, reopen, sharing, constraint, cascade, and preservation tests pass.

## Step 2 — Sidecar parser and manual mappings

**Status: Done**

- Extend sidecar parsing to accept `[rpi-streamer]`, `[work "NAME"]`, and
  `[media "NAME"]` sections while retaining interpolation-free UTF-8 parsing.
- Preserve option case where needed and compare `file` values to exact
  basenames; never interpret them as paths.
- Parse comma-separated `related_mal_ids`, multiline globs, positive ranges,
  signed offsets, kinds, order, labels, and exact overrides with strict bounds.
- Limit section count, patterns per work, total pattern characters, and manual
  candidates per folder to prevent pathological configuration.
- Compile globs into bounded standard-library matching; reject path separators,
  NULs, absolute paths, `..`, and patterns that can match outside the current
  basename set.
- Reconcile sidecar changes by digest:
  - exact manual mappings update immediately;
  - removed rules remove only mappings derived from those rules;
  - unchanged rules retain stable rows;
  - manual changes invalidate conflicting lower-precedence cached model
    mappings without deleting the shared inference result.
- Validate every manual MAL ID through cached or live Tenrai details before
  treating it as verified. If offline and not cached, retain the declared rule
  as pending and leave affected files unmapped rather than discarding the last
  known-good mapping.

**Tests**

- Old sidecars, multiple work sections, multiline globs, season selectors,
  cumulative ranges/offsets, exact overrides, Unicode filenames, whitespace,
  duplicate sections/files, unknown keys, overlapping rules, missing files,
  invalid IDs/ranges/offsets/kinds, excessive rules, and offline verification.
- MF Ghost maps 1–12, 13–24, and 25–37 to three works with episode numbers
  reset by offsets.
- Tsukimichi maps parsed season 1 and season 2 to separate works.
- An exact media override wins over every work rule.

**Documentation/commit:** publish the final sidecar grammar and examples;
commit as `feat: support manual multi-work mappings`.

**Completion:** the UTF-8 interpolation-free parser implements the frozen
root/work/media grammar and bounds. Manual IDs use normalized cache records or
the configured live provider verifier; unavailable declarations remain
pending. Exact overrides and AND-combined work selectors reconcile by
canonical digest, preserve unchanged rows, remove stale manual results, and
supersede lower-precedence mappings. Malformed, missing, overlapping, or
offline rules produce bounded partial-scan issues without discarding the last
valid mapping. The cumulative 37-file and reset-numbered 12+25 fixtures map
entirely from manual rules.

## Step 3 — Tenrai relation discovery and candidates

**Status: Done**

- Starting from the verified primary work, traverse only anime relations with
  allowed types: sequel, prequel, side story, parent story, spin-off, summary,
  alternative version, and other explicitly reviewed tie-ins.
- Follow a bounded graph:
  - default maximum depth `3`;
  - maximum `12` candidate works per collection;
  - cycle detection by logical provider and MAL ID;
  - shared provider throttling/retry limits;
  - stop cleanly on partial provider failure.
- Fetch details for candidates lazily and cache normalized records. Prefer
  direct sequel chains when local evidence contains multiple seasons.
- Merge candidates from:
  - primary verified match;
  - sidecar `related_mal_ids`;
  - manual work/media sections;
  - bounded Tenrai relations;
  - deterministic search only when no primary work exists.
- Store association provenance and relation distance. Do not allow the LLM to
  add IDs or cause unbounded provider searches.
- Add a cheap multi-work suspicion detector:
  - parsed season greater than one;
  - episode numbering reset;
  - local maximum/count exceeds the primary provider episode count;
  - basename markers such as `2nd Season`, `Movie`, `OVA`, `Special`, or
    `Digest`;
  - relevant provider relations.
- Skip graph expansion for ordinary single-work folders unless manual
  candidates or suspicion justify it.

**Tests**

- Sequel chains, cycles, duplicate relations, non-anime relations, depth/count
  limits, cached candidates, transient errors, offline cache, and unrelated
  tie-ins.
- The MF Ghost fixture discovers enough sequel candidates for three seasons.
- The Tsukimichi fixture discovers its second season.
- Candidate discovery makes no OpenAI calls.

**Documentation/commit:** document relation bounds and offline behavior;
commit as `feat: discover related metadata works`.

**Completion:** candidate discovery starts from the verified primary work,
merges existing manual associations, prioritizes sequel edges, filters to the
reviewed anime relation vocabulary, and records relation source/distance.
Traversal is cycle-safe and capped at depth 3 and 12 total collection works.
The cheap suspicion gate skips ordinary folders; normalized records provide
offline traversal; missing records use the shared throttled verifier; and
partial provider failures retain the prior candidate set. MF Ghost and
Tsukimichi fixture graphs discover all expected seasons without OpenAI.

## Step 4 — Deterministic mapping engine

**Status: Pending**

- Represent parsed local media facts independently from display hints:
  season, episode start/end, special kind, explicit ordinal, and basename
  markers.
- Extend conservative parsing for:
  - `1st`/`2nd`/`3rd Season`;
  - `Season 2`, `S02E03`, and current variants;
  - reset numbering and cumulative numbering;
  - movie, OVA, OAD, ONA, special, summary/digest;
  - misleading resolution, year, codec, release-group, and duplicate `.mp4`
    text.
- Apply manual selectors first, then infer work boundaries only when supported
  by verified candidate episode counts and filenames.
- For cumulative numbering, permit an automatic offset only when contiguous
  boundaries align with candidate episode counts and the result is unique.
- For reset numbering, use explicit season markers and candidate sequel order.
- Never assign by episode count alone when two candidate layouts fit.
- Validate mapped episode ranges against provider episode counts when known.
  Specials and currently airing/unknown-count works use conservative bounds.
- Persist deterministic provenance and a digest of filename facts, candidates,
  parser version, and rules. Recompute only affected mappings.
- Produce explicit outcomes: mapped, ambiguous, invalid, pending provider, or
  unmapped.

**Tests**

- MF Ghost maps three cumulative ranges using verified work boundaries without
  an LLM when the candidate counts uniquely support them.
- Tsukimichi maps both seasons using reset numbering and `2nd Season`.
- Fixtures cover specials, movies, ranges, misleading `1080p`/years, missing
  episodes, non-contiguous batches, duplicate numbering, ambiguous split
  points, renamed files, and provider count changes.
- Existing single-title parsing and natural ordering remain compatible.

**Documentation/commit:** explain deterministic mapping and ambiguity;
commit as `feat: map local files across related works`.

## Step 5 — Structured LLM-assisted mapping

**Status: Pending**

Extend the existing optional OpenAI client only after deterministic mapping
has left unresolved files.

- Introduce a new versioned multi-work inference schema rather than changing
  cached v1 episode results in place.
- Send only:
  - bounded collection display/directory name;
  - unresolved exact basenames and parsed local facts;
  - bounded verified candidates containing MAL ID, preferred title, media
    type, episode count, relation type/distance, and order;
  - existing manual rule names only when needed to explain exclusions.
- Continue excluding MP4 content, absolute paths, sidecar raw text, database
  rows, API keys, unbounded provider payloads, and synopsis text.
- Require one structured entry per submitted filename:
  - exact filename;
  - `mal_id` chosen from a request-specific enum or `null`;
  - kind;
  - episode start/end or `null`;
  - confidence;
  - short bounded reason code/text.
- Instruct the model to prefer `null` over guessing and never reinterpret an
  exact manual mapping.
- Validate after Structured Outputs:
  - exact submitted filename and uniqueness;
  - MAL ID membership in the supplied candidate set;
  - allowed kind and numeric bounds;
  - provider episode count where known;
  - manual/deterministic precedence;
  - confidence threshold, initially `0.85`;
  - complete response and schema version.
- Cache by a privacy-preserving digest of model, schema, unresolved filenames,
  parsed facts, candidate identities/versions, and applicable rule digest.
- Cache accepted and stable uncertain results. Retry transient transport
  failures later with cooldown; never spend another call on an unchanged valid
  cache entry.
- Keep the existing per-scan call budget. Batch one collection per call within
  filename/candidate/token limits and process overflow deterministically in
  later scans rather than silently truncating it.
- Store accepted model mappings separately with model, schema, confidence, and
  input digest. A manual change supersedes them immediately.

**Tests**

- Strict request schema, candidate enum, minimal input, privacy boundaries,
  refusal, malformed/incomplete output, invented IDs, unknown/duplicate
  filenames, impossible episodes, low confidence, call budget, cache hits,
  invalidation, timeout/quota/rate-limit failure, and disabled mode.
- The model resolves a deliberately ambiguous tie-in only from verified
  candidates.
- Deterministic and fully manual fixtures make zero model calls.
- All tests use fake transports; an opt-in live smoke test requires an
  explicit flag and key.

**Documentation/commit:** update privacy, cost, cache, and failure guidance and
ADR 0005; commit as `feat: infer multi-work media mappings`.

## Step 6 — Grouped static catalogue

**Status: Pending**

- Keep one HTML5 player and the existing previous/next/select behavior.
- Render `<optgroup>` groups for each associated work, ordered by manual order,
  verified sequel order, then normalized provider title/MAL ID.
- Label options with the work-local episode:
  `Episode 1`, `OVA 1`, `Movie`, `Summary`, or the original filename when
  unmapped. Retain the exact filename in visible secondary text and no-JS
  links.
- Display the selected work title and provider episode title near the player.
- Retain the collection-level header, primary work cover, synopsis, and genre
  summary initially. Add compact work cards for other associated works with
  cover, type, episode count, relation, and mapping confidence/provenance where
  useful.
- Render provider episode context per work rather than showing only the
  primary 12-row table.
- Add an `Unmapped` group for uncertain files; never hide or disable them.
- Preserve stable collection slug, media URLs, fragments where possible,
  escaping, asset hashing, atomic publication, and last-known-good rollback.
- Update JavaScript to navigate across group boundaries while keeping
  first/last states and deep links correct.

**Tests**

- DOM/snapshot fixtures for three seasons, reset numbering, movie/OVA/summary,
  unmapped files, missing metadata/artwork, Unicode, dangerous filenames, and
  no JavaScript.
- Exactly one player exists and only the selected source is loaded.
- Previous/next crosses work boundaries predictably.
- Existing single-work snapshots change only where the new markup is
  intentionally universal.
- Nginx byte ranges and media URLs remain unchanged.

**Documentation/commit:** add grouped-page examples and accessibility notes;
commit as `feat: group collection media by metadata work`.

## Step 7 — Operator controls, observability, and documentation

**Status: Pending**

- Add a read-only CLI command to inspect one collection's:
  primary work, candidates, manual rules, per-file mapping, provenance,
  confidence, cache state, and ambiguity/error reasons.
- Add bounded commands/options to:
  - refresh candidate relations for one collection;
  - invalidate only model mappings/cache for one collection;
  - recompute deterministic mappings;
  - perform a dry-run sidecar validation.
- Do not make ordinary `SIGHUP` bypass valid caches or automatically spend
  OpenAI calls beyond the configured budget.
- Add sanitized logs and scan counters for:
  suspected multi-work collections, candidates discovered, manual mappings,
  deterministic mappings, model mappings, cache hits, ambiguous/unmapped
  files, conflicts, and provider/model failures.
- Never log raw sidecars, full prompts/responses, API keys, or uncontrolled
  filename lists. Reuse printable/bounded log sanitization.
- Update:
  - README layout, sidecar reference, mapping precedence, grouped UI, privacy,
    troubleshooting, backup, upgrade, and container/native examples;
  - CHANGELOG;
  - ADR 0002, 0003, 0004, and 0005 where consequences change;
  - a new ADR 0008 as the authoritative feature decision.
- Explain how to discover real MAL IDs and replace placeholder IDs safely.

**Tests**

- CLI output is deterministic, redacted, bounded, and returns useful exit
  codes for unknown collections and invalid sidecars.
- Refresh/invalidation affects only the selected collection and never deletes
  media/provider metadata unnecessarily.
- Documentation examples parse under the real sidecar parser.

**Commit:** `feat: add multi-work mapping controls`.

## Step 8 — End-to-end and Raspberry Pi acceptance

**Status: Pending**

- Run the complete offline pipeline for both supplied layouts:
  discovery, sidecar parsing, migration, provider fixtures, candidate graph,
  deterministic/model mapping, SQLite persistence, static generation,
  rescan, rename/removal, and no-JS output.
- Exercise upgrade from a real schema-v5 database containing single-title
  metadata and OpenAI inference cache.
- Test failure boundaries:
  migration rollback, malformed sidecar, unavailable Tenrai, partial relation
  graph, OpenAI disabled/refused/timed out, generation failure, interrupted
  update, and stale cached site.
- Verify container and native configuration remain compatible and no new
  runtime dependency is introduced without explicit justification.
- On the Raspberry Pi:
  - back up `/etc/rpi-streamer` and `/var/lib/rpi-streamer`;
  - deploy through `make update`;
  - validate the existing simple-title collection is unchanged;
  - scan MF Ghost and Tsukimichi cold and cached;
  - inspect automatic mappings and add at least one manual correction;
  - confirm grouped browser navigation, MP4 playback, seeking, and no-JS links;
  - send `SIGHUP` and confirm valid provider/model caches are reused;
  - record provider and OpenAI call counts, elapsed time, peak RSS, database
    growth, unmapped count, and incorrect mapping count.
- Temporarily disable OpenAI and Tenrai independently to prove manual mappings,
  cached metadata, generated pages, and playback continue working.
- Restore or correct any test sidecars; do not modify the media files.

**Acceptance**

- MF Ghost displays three work groups with season-local episode numbering.
- Tsukimichi displays first and second season groups with provider context for
  both.
- Manual exact and range corrections win on the next rescan.
- No unsupported or uncertain file is hidden.
- Existing single-title pages and URLs remain functional.
- Offline tests, Ruff, formatting, strict mypy, Nginx checks where installed,
  native deployment audits, and container configuration all pass.

**Documentation/commit:** record measured acceptance without personal paths,
credentials, or media; commit as `chore: complete multi-work acceptance`.

## Cross-cutting quality rules

- Prefer the standard library and existing provider/inference boundaries.
- Keep ordinary tests offline and use synthetic filenames and provider data.
- Treat every path, basename, sidecar value, provider field, and model field as
  untrusted.
- Preserve media as read-only in Python, systemd, and containers.
- Keep provider/model requests bounded, cached, serialized, and non-fatal.
- Preserve last-known-good database state and generated output on every
  failure.
- Add forward migration and rollback tests before changing persisted
  contracts.
- Never expose API keys, prompts, full responses, personal paths, or raw
  sidecars in logs or generated pages.
- Update README, relevant ADRs, changelog, tests, and this status table in the
  same commit as each increment.
- Keep each implementation commit focused and leave the working tree clean.

## Definition of done

This feature is done only when:

1. all eight increments are marked Done with evidence;
2. existing schema-v5 and single-title behavior migrate safely;
3. both supplied multi-season layouts pass end-to-end fixtures;
4. manual mappings work without OpenAI or Tenrai availability;
5. model mappings are restricted to verified candidates and remain optional;
6. grouped pages preserve playback, seeking, accessibility, stable URLs, and
   unmapped files;
7. automated checks pass and Raspberry Pi evidence is recorded;
8. documentation and ADRs describe shipped behavior rather than future intent.
