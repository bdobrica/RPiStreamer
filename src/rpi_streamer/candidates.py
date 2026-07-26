"""Bounded, verified related-work discovery for multi-work collections."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from rpi_streamer.database import (
    CatalogueRepository,
    LibraryEntry,
    ProviderRecord,
    Relation,
)

MAX_RELATION_DEPTH: Final = 3
MAX_CANDIDATE_WORKS: Final = 12
ALLOWED_RELATIONS: Final = frozenset(
    {
        "sequel",
        "prequel",
        "side story",
        "parent story",
        "spin off",
        "summary",
        "alternative version",
        "other",
    }
)
_RELATION_PRIORITY: Final = {
    "sequel": 0,
    "prequel": 1,
    "parent story": 2,
    "side story": 3,
    "spin off": 4,
    "summary": 5,
    "alternative version": 6,
    "other": 7,
}
_SEASON_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:s(?:eason)?[ ._-]*0*([2-9]\d*)|"
    r"([2-9]\d*)(?:nd|rd|th)[ ._-]*season)",
    re.IGNORECASE,
)
_EPISODE_RE: Final = re.compile(r"(?:^|[ ._-])(\d{1,4})(?=$|[ ._-])", re.IGNORECASE)
_TIE_IN_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(movie|ova|oad|ona|special|digest|summary)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

WorkVerifier = Callable[
    [CatalogueRepository, str, datetime], tuple[ProviderRecord | None, str | None]
]


@dataclass(frozen=True, slots=True)
class Suspicion:
    suspected: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateDiscovery:
    associated: int
    expanded: bool
    reasons: tuple[str, ...]
    errors: tuple[str, ...]


def multi_work_suspicion(
    filenames: Sequence[str],
    primary: ProviderRecord,
    relations: Sequence[Relation],
    *,
    has_manual_candidates: bool,
) -> Suspicion:
    """Return cheap, explainable evidence that a folder may span works."""

    reasons: list[str] = []
    if has_manual_candidates:
        reasons.append("manual candidates")
    if any(_SEASON_RE.search(filename) for filename in filenames):
        reasons.append("season greater than one")
    if any(_TIE_IN_RE.search(filename) for filename in filenames):
        reasons.append("tie-in marker")

    episodes = [
        number
        for filename in filenames
        if (number := _episode_number(filename)) is not None
    ]
    if len(episodes) != len(set(episodes)):
        reasons.append("episode numbering reset")
    if primary.episode_count is not None and (
        len(filenames) > primary.episode_count
        or (episodes and max(episodes) > primary.episode_count)
    ):
        reasons.append("local episodes exceed primary count")
    if any(_allowed_relation(relation) for relation in relations):
        reasons.append("relevant provider relation")
    return Suspicion(bool(reasons), tuple(dict.fromkeys(reasons)))


def discover_related_candidates(
    repository: CatalogueRepository,
    entry: LibraryEntry,
    filenames: Sequence[str],
    *,
    has_manual_candidates: bool,
    verified_at: datetime,
    verify_work: WorkVerifier | None,
    max_depth: int = MAX_RELATION_DEPTH,
    max_candidates: int = MAX_CANDIDATE_WORKS,
) -> CandidateDiscovery:
    """Traverse a bounded verified relation graph from the primary work."""

    if not 1 <= max_depth <= MAX_RELATION_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_RELATION_DEPTH}")
    if not 1 <= max_candidates <= MAX_CANDIDATE_WORKS:
        raise ValueError(f"max_candidates must be between 1 and {MAX_CANDIDATE_WORKS}")
    primary_work = repository.get_primary_library_entry_work(entry.id)
    if primary_work is None:
        return CandidateDiscovery(0, False, (), ())
    primary = repository.get_provider_record_for_work(primary_work.id)
    if primary is None:
        return CandidateDiscovery(0, False, (), ())
    primary_relations = repository.list_relations(primary.id)
    suspicion = multi_work_suspicion(
        filenames,
        primary,
        primary_relations,
        has_manual_candidates=has_manual_candidates,
    )
    if not suspicion.suspected:
        repository.remove_stale_relation_work_associations(entry.id, set())
        return CandidateDiscovery(0, False, suspicion.reasons, ())

    base_works = [
        work
        for work in repository.list_library_entry_works(entry.id)
        if work.source != "relation"
    ]
    visited = {
        (record.provider, record.provider_id)
        for work in base_works
        if (record := repository.get_provider_record_for_work(work.id)) is not None
    }
    visited.add((primary.provider, primary.provider_id))
    queue: deque[tuple[ProviderRecord, int]] = deque([(primary, 0)])
    retained: set[int] = set()
    errors: list[str] = []
    associated = 0
    complete = True

    while queue and len(visited) < max_candidates:
        source, depth = queue.popleft()
        if depth >= max_depth:
            continue
        relations = sorted(
            (
                relation
                for relation in repository.list_relations(source.id)
                if _allowed_relation(relation)
            ),
            key=_relation_key,
        )
        for relation in relations:
            identity = (relation.target_provider, relation.target_provider_id)
            if identity in visited:
                continue
            if len(visited) >= max_candidates:
                break
            visited.add(identity)
            record = repository.get_provider_record_by_provider_id(*identity)
            error: str | None = None
            if record is None and verify_work is not None:
                record, error = verify_work(
                    repository, relation.target_provider_id, verified_at
                )
            if record is None:
                complete = False
                errors.append(
                    f"{entry.relative_path}: related MAL ID "
                    f"{relation.target_provider_id} unavailable: "
                    f"{error or 'not cached and provider unavailable'}"
                )
                continue
            distance = depth + 1
            existing = repository.get_library_entry_work_by_provider_id(
                entry.id, record.provider, record.provider_id
            )
            if existing is None or existing.source == "relation":
                repository.associate_library_entry_work(
                    library_entry_id=entry.id,
                    provider_record_id=record.id,
                    local_name=f"relation-{record.provider_id}",
                    source="relation",
                    verified_at=verified_at,
                    display_order=1000 + distance * 100 + associated,
                    relation_distance=distance,
                )
                associated += int(existing is None)
            retained.add(record.id)
            queue.append((record, distance))

    if complete:
        repository.remove_stale_relation_work_associations(entry.id, retained)
    return CandidateDiscovery(
        associated, True, suspicion.reasons, tuple(dict.fromkeys(errors))
    )


def _allowed_relation(relation: Relation) -> bool:
    return (
        relation.target_provider == "jikan"
        and _normalized_relation(relation.relation_type) in ALLOWED_RELATIONS
    )


def _normalized_relation(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())


def _relation_key(relation: Relation) -> tuple[int, str, str]:
    normalized = _normalized_relation(relation.relation_type)
    return (
        _RELATION_PRIORITY.get(normalized, len(_RELATION_PRIORITY)),
        relation.target_provider_id,
        relation.target_title.casefold(),
    )


def _episode_number(filename: str) -> int | None:
    matches = list(_EPISODE_RE.finditer(filename.rsplit(".", 1)[0]))
    if not matches:
        return None
    values = [int(match.group(1)) for match in matches]
    plausible = [value for value in values if value < 1000]
    return plausible[-1] if plausible else None
