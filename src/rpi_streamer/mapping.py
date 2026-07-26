"""Conservative filename parsing and deterministic multi-work mapping."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from rpi_streamer.database import (
    CatalogueRepository,
    LibraryEntryWork,
    MediaFile,
    ProviderRecord,
)

PARSER_VERSION: Final = "1"
MAPPING_SCHEMA_VERSION: Final = "1"
MappingOutcome = Literal[
    "mapped", "ambiguous", "invalid", "pending provider", "unmapped"
]

_SEASON_EPISODE_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])S0*(\d{1,3})[ ._-]*E0*(\d{1,4})"
    r"(?:[ ._-]*(?:-|~)[ ._-]*(?:E)?0*(\d{1,4}))?",
    re.IGNORECASE,
)
_ORDINAL_SEASON_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])0*(\d{1,3})(?:st|nd|rd|th)[ ._-]*season",
    re.IGNORECASE,
)
_NAMED_SEASON_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])season[ ._-]*0*(\d{1,3})(?!\d)", re.IGNORECASE
)
_EPISODE_RE: Final = re.compile(
    r"(?:^|[ ._-])(?:ep(?:isode)?[ ._-]*)?0*(\d{1,4})"
    r"(?:[ ._-]*(?:-|~)[ ._-]*0*(\d{1,4}))?(?=$|[ ._-])",
    re.IGNORECASE,
)
_SPECIAL_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(movie|ova|oad|ona|special|sp|summary|digest|recap)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NOISE_RE: Final = re.compile(
    r"^(?:19\d{2}|20\d{2}|[248]k|480|576|720|1080|2160|264|265)$",
    re.IGNORECASE,
)
_CODEC_RE: Final = re.compile(r"^(?:x|h)?26[45]$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LocalMediaFacts:
    """Filename evidence used for mapping, separate from presentation hints."""

    filename: str
    basename: str
    season: int | None
    episode_start: int | None
    episode_end: int | None
    special_kind: str | None
    explicit_ordinal: int | None
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MappingDecision:
    media_file_id: int
    filename: str
    outcome: MappingOutcome
    work_id: int | None = None
    kind: str = "unknown"
    episode_start: int | None = None
    episode_end: int | None = None
    reason: str | None = None
    input_digest: str = ""


@dataclass(frozen=True, slots=True)
class DeterministicMappingResult:
    decisions: tuple[MappingDecision, ...]

    @property
    def mapped(self) -> int:
        return sum(item.outcome == "mapped" for item in self.decisions)


def parse_local_media_facts(filename: str) -> LocalMediaFacts:
    """Parse only strong filename signals, ignoring common release noise."""

    basename = Path(filename).name
    stem = re.sub(r"(?:\.mp4)+$", "", basename, flags=re.IGNORECASE)
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    explicit_ordinal: int | None = None

    season_episode = _SEASON_EPISODE_RE.search(stem)
    if season_episode:
        season = int(season_episode.group(1))
        episode_start = int(season_episode.group(2))
        episode_end = int(season_episode.group(3) or episode_start)
        explicit_ordinal = season
    else:
        season_match = _ORDINAL_SEASON_RE.search(stem)
        if season_match is None:
            season_match = _NAMED_SEASON_RE.search(stem)
        if season_match:
            season = int(season_match.group(1))
            explicit_ordinal = season
        candidates: list[tuple[int, int]] = []
        for match in _EPISODE_RE.finditer(stem):
            raw_start = match.group(1)
            raw_end = match.group(2)
            token = raw_start.casefold()
            before = stem[max(0, match.start() - 1) : match.start()]
            if _NOISE_RE.match(token) or _CODEC_RE.match(token):
                continue
            # A four-digit unlabelled token is overwhelmingly a year/resolution.
            if len(raw_start) == 4 and before.casefold() not in {"e"}:
                continue
            start = int(raw_start)
            end = int(raw_end or start)
            if 1 <= start <= 999 and start <= end <= 9999:
                candidates.append((start, end))
        if candidates:
            episode_start, episode_end = candidates[-1]

    special_match = _SPECIAL_RE.search(stem)
    special_kind = None
    if special_match:
        special_kind = {
            "sp": "special",
            "recap": "summary",
            "digest": "summary",
        }.get(special_match.group(1).casefold(), special_match.group(1).casefold())
    markers = tuple(
        dict.fromkeys(
            part.casefold()
            for part in re.split(r"[^A-Za-z0-9]+", stem)
            if part
            and not _NOISE_RE.match(part)
            and not _CODEC_RE.match(part)
            and not part.isdigit()
        )
    )
    return LocalMediaFacts(
        filename=filename,
        basename=stem,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        special_kind=special_kind,
        explicit_ordinal=explicit_ordinal,
        markers=markers,
    )


def map_entry_deterministically(
    repository: CatalogueRepository,
    library_entry_id: int,
    *,
    mapped_at: datetime,
    rules_digest: str,
) -> DeterministicMappingResult:
    """Map files only when verified counts and filename evidence are unique."""

    media = repository.list_media_files(library_entry_id)
    facts_by_id = {item.id: parse_local_media_facts(item.filename) for item in media}
    works = _ordered_works(repository, library_entry_id)
    records = {
        work.id: repository.get_provider_record_for_work(work.id) for work in works
    }
    candidate_payload: list[dict[str, object]] = []
    for work in works:
        record = records[work.id]
        candidate_payload.append(
            {
                "work_id": work.id,
                "provider_id": record.provider_id if record else None,
                "episodes": record.episode_count if record else None,
                "primary": work.is_primary,
                "distance": work.relation_distance,
            }
        )
    decisions = _decide(media, facts_by_id, works, records)
    retained: set[int] = set()
    final: list[MappingDecision] = []
    for decision in decisions:
        facts = facts_by_id[decision.media_file_id]
        digest = _input_digest(rules_digest, facts, candidate_payload)
        decision = MappingDecision(
            **{
                **asdict(decision),
                "input_digest": digest,
            }
        )
        current = repository.get_media_work_mapping(decision.media_file_id)
        if current is not None and current.source.startswith("manual_"):
            final.append(
                MappingDecision(
                    decision.media_file_id,
                    decision.filename,
                    "mapped",
                    current.library_entry_work_id,
                    current.kind,
                    current.episode_start,
                    current.episode_end,
                    "manual mapping has precedence",
                    current.input_digest,
                )
            )
            continue
        if decision.outcome == "mapped" and decision.work_id is not None:
            retained.add(decision.media_file_id)
            if not (
                current is not None
                and current.source == "deterministic"
                and current.library_entry_work_id == decision.work_id
                and current.kind == decision.kind
                and current.episode_start == decision.episode_start
                and current.episode_end == decision.episode_end
                and current.input_digest == digest
            ):
                repository.set_media_work_mapping(
                    media_file_id=decision.media_file_id,
                    library_entry_work_id=decision.work_id,
                    kind=decision.kind,
                    episode_start=decision.episode_start,
                    episode_end=decision.episode_end,
                    source="deterministic",
                    schema_version=MAPPING_SCHEMA_VERSION,
                    input_digest=digest,
                    mapped_at=mapped_at,
                )
        final.append(decision)
    repository.remove_stale_deterministic_mappings(library_entry_id, retained)
    return DeterministicMappingResult(tuple(final))


def _decide(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    works: Sequence[LibraryEntryWork],
    records: dict[int, ProviderRecord | None],
) -> list[MappingDecision]:
    if not works:
        return [
            _outcome(item, "pending provider", "no verified primary work")
            for item in media
        ]
    if any(records[work.id] is None for work in works):
        return [
            _outcome(item, "pending provider", "candidate metadata is unavailable")
            for item in media
        ]

    specials = [item for item in media if facts_by_id[item.id].special_kind]
    episodic = [item for item in media if not facts_by_id[item.id].special_kind]
    special_decisions = _map_specials(specials, facts_by_id, works, records)
    episode_decisions = _decide_episodes(episodic, facts_by_id, works, records)
    by_id = {
        decision.media_file_id: decision
        for decision in (*special_decisions, *episode_decisions)
    }
    return [by_id[item.id] for item in media]


def _decide_episodes(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    works: Sequence[LibraryEntryWork],
    records: dict[int, ProviderRecord | None],
) -> list[MappingDecision]:
    if not media:
        return []
    explicit_seasons = {
        facts_by_id[item.id].season
        for item in media
        if facts_by_id[item.id].season is not None
    }
    if any(season is not None and season > 1 for season in explicit_seasons):
        return _map_reset(media, facts_by_id, works, records)

    episodic = [
        facts_by_id[item.id]
        for item in media
        if facts_by_id[item.id].episode_start is not None
    ]
    numbers = [
        facts.episode_start for facts in episodic if facts.episode_start is not None
    ]
    if len(numbers) != len(set(numbers)):
        return [
            _outcome(item, "ambiguous", "duplicate episode numbering") for item in media
        ]
    if numbers and sorted(numbers) == list(range(1, max(numbers) + 1)):
        prefixes: list[Sequence[LibraryEntryWork]] = []
        running = 0
        for index, work in enumerate(works):
            count = records[work.id].episode_count  # type: ignore[union-attr]
            if count is None:
                break
            running += count
            if running == max(numbers):
                prefixes.append(works[: index + 1])
        if len(prefixes) == 1:
            return _map_cumulative(media, facts_by_id, prefixes[0], records)
        if len(prefixes) > 1:
            return [
                _outcome(item, "ambiguous", "multiple cumulative layouts fit")
                for item in media
            ]

    if len(works) == 1:
        return _map_single(media, facts_by_id, works[0], records[works[0].id])
    reason = "episode sequence is incomplete or candidate boundaries do not align"
    return [_outcome(item, "unmapped", reason) for item in media]


def _map_specials(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    works: Sequence[LibraryEntryWork],
    records: dict[int, ProviderRecord | None],
) -> list[MappingDecision]:
    decisions: list[MappingDecision] = []
    for item in media:
        facts = facts_by_id[item.id]
        assert facts.special_kind is not None
        if len(works) == 1:
            decisions.append(_mapped(item, facts, works[0].id))
            continue
        aliases = {
            "summary": {"summary", "digest", "recap"},
            "special": {"special", "sp"},
        }.get(facts.special_kind, {facts.special_kind})
        matches = [
            work
            for work in works
            if records[work.id] is not None
            and aliases.intersection(
                re.split(
                    r"[^a-z0-9]+",
                    records[work.id].canonical_title.casefold(),  # type: ignore[union-attr]
                )
            )
        ]
        if len(matches) == 1:
            decisions.append(_mapped(item, facts, matches[0].id))
        elif len(matches) > 1:
            decisions.append(
                _outcome(item, "ambiguous", "multiple special-kind candidates match")
            )
        else:
            decisions.append(
                _outcome(item, "unmapped", "no unique special-kind candidate")
            )
    return decisions


def _map_reset(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    works: Sequence[LibraryEntryWork],
    records: dict[int, ProviderRecord | None],
) -> list[MappingDecision]:
    decisions: list[MappingDecision] = []
    has_later = any((facts.season or 0) > 1 for facts in facts_by_id.values())
    for item in media:
        facts = facts_by_id[item.id]
        season = facts.season or (1 if has_later and facts.episode_start else None)
        if season is None or not 1 <= season <= len(works):
            decisions.append(_outcome(item, "unmapped", "no verified season ordinal"))
            continue
        work = works[season - 1]
        record = records[work.id]
        if facts.episode_start is None:
            decisions.append(_outcome(item, "unmapped", "no episode number"))
        elif (
            record is not None
            and record.episode_count is not None
            and facts.episode_end is not None
            and facts.episode_end > record.episode_count
        ):
            decisions.append(
                _outcome(item, "invalid", "episode exceeds provider count")
            )
        else:
            decisions.append(_mapped(item, facts, work.id))
    return decisions


def _map_cumulative(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    works: Sequence[LibraryEntryWork],
    records: dict[int, ProviderRecord | None],
) -> list[MappingDecision]:
    boundaries: list[tuple[int, int, LibraryEntryWork]] = []
    start = 1
    for work in works:
        count = records[work.id].episode_count  # type: ignore[union-attr]
        assert count is not None
        boundaries.append((start, start + count - 1, work))
        start += count
    decisions: list[MappingDecision] = []
    for item in media:
        facts = facts_by_id[item.id]
        if facts.episode_start is None or facts.episode_end is None:
            decisions.append(_outcome(item, "unmapped", "no episode number"))
            continue
        matching = [
            boundary
            for boundary in boundaries
            if boundary[0] <= facts.episode_start <= facts.episode_end <= boundary[1]
        ]
        if len(matching) != 1:
            decisions.append(
                _outcome(item, "invalid", "episode range crosses a work boundary")
            )
            continue
        boundary_start, _, work = matching[0]
        decisions.append(
            _mapped(
                item,
                facts,
                work.id,
                episode_start=facts.episode_start - boundary_start + 1,
                episode_end=facts.episode_end - boundary_start + 1,
            )
        )
    return decisions


def _map_single(
    media: Sequence[MediaFile],
    facts_by_id: dict[int, LocalMediaFacts],
    work: LibraryEntryWork,
    record: ProviderRecord | None,
) -> list[MappingDecision]:
    decisions: list[MappingDecision] = []
    for item in media:
        facts = facts_by_id[item.id]
        if facts.special_kind is not None:
            decisions.append(_mapped(item, facts, work.id))
        elif facts.episode_start is None:
            decisions.append(_outcome(item, "unmapped", "no episode number"))
        elif (
            record is not None
            and record.episode_count is not None
            and facts.episode_end is not None
            and facts.episode_end > record.episode_count
        ):
            decisions.append(
                _outcome(item, "invalid", "episode exceeds provider count")
            )
        else:
            decisions.append(_mapped(item, facts, work.id))
    return decisions


def _mapped(
    media: MediaFile,
    facts: LocalMediaFacts,
    work_id: int,
    *,
    episode_start: int | None = None,
    episode_end: int | None = None,
) -> MappingDecision:
    kind = facts.special_kind or (
        "episode" if facts.episode_start is not None else "unknown"
    )
    return MappingDecision(
        media.id,
        media.filename,
        "mapped",
        work_id,
        kind,
        facts.episode_start if episode_start is None else episode_start,
        facts.episode_end if episode_end is None else episode_end,
    )


def _outcome(media: MediaFile, outcome: MappingOutcome, reason: str) -> MappingDecision:
    return MappingDecision(media.id, media.filename, outcome, reason=reason)


def _ordered_works(
    repository: CatalogueRepository, library_entry_id: int
) -> list[LibraryEntryWork]:
    works = repository.list_library_entry_works(library_entry_id)
    return sorted(
        works,
        key=lambda work: (
            not work.is_primary,
            work.relation_distance if work.relation_distance is not None else 0,
            work.display_order,
            work.id,
        ),
    )


def _input_digest(
    rules_digest: str,
    facts: LocalMediaFacts,
    candidates: list[dict[str, object]],
) -> str:
    payload = {
        "parser_version": PARSER_VERSION,
        "mapping_schema_version": MAPPING_SCHEMA_VERSION,
        "rules_digest": rules_digest,
        "facts": asdict(facts),
        "candidates": candidates,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
