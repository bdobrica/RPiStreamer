"""Bounded operator inspection and controls for multi-work collections."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rpi_streamer.candidates import WorkVerifier, discover_related_candidates
from rpi_streamer.database import CatalogueRepository, LibraryEntry
from rpi_streamer.mapping import (
    map_entry_deterministically,
    preview_entry_deterministically,
)
from rpi_streamer.scanner import SIDECAR_NAME
from rpi_streamer.sidecar import Sidecar, read_sidecar, validate_local_files

MAX_INSPECTED_FILES: Final = 200


class CollectionNotFoundError(LookupError):
    """Raised when an exact collection path is not present in the catalogue."""


def inspect_collection(
    repository: CatalogueRepository,
    media_root: Path,
    collection: str,
) -> dict[str, object]:
    """Return deterministic, bounded, secret-free mapping diagnostics."""

    entry = _entry(repository, collection)
    sidecar, sidecar_error = _sidecar(media_root, entry, repository)
    works = repository.list_library_entry_works(entry.id)
    records = {
        work.id: repository.get_provider_record_for_work(work.id) for work in works
    }
    mappings = {
        item.media_file_id: item
        for item in repository.list_media_work_mappings(entry.id)
    }
    decisions = {
        item.media_file_id: item
        for item in preview_entry_deterministically(repository, entry.id).decisions
    }
    media = repository.list_media_files(entry.id)
    truncated = len(media) > MAX_INSPECTED_FILES
    files = []
    for item in media[:MAX_INSPECTED_FILES]:
        mapping = mappings.get(item.id)
        work = (
            next(
                (
                    candidate
                    for candidate in works
                    if mapping is not None
                    and candidate.id == mapping.library_entry_work_id
                ),
                None,
            )
            if mapping is not None
            else None
        )
        record = records.get(work.id) if work is not None else None
        decision = decisions.get(item.id)
        cache = (
            repository.get_inference_cache(mapping.input_digest)
            if mapping is not None and mapping.source == "model"
            else None
        )
        files.append(
            {
                "filename": _bounded(item.filename, 300),
                "mal_id": None if record is None else record.provider_id,
                "work": None if record is None else record.canonical_title,
                "kind": None if mapping is None else mapping.kind,
                "episode_start": None if mapping is None else mapping.episode_start,
                "episode_end": None if mapping is None else mapping.episode_end,
                "source": None if mapping is None else mapping.source,
                "confidence": None if mapping is None else mapping.confidence,
                "cache": "present" if cache is not None else "none",
                "outcome": "unmapped" if decision is None else decision.outcome,
                "reason": (
                    "no deterministic decision"
                    if decision is None
                    else _bounded(decision.reason, 160)
                ),
            }
        )
    primary = next((work for work in works if work.is_primary), None)
    primary_record = None if primary is None else records[primary.id]
    candidates = []
    for work in works:
        record = records[work.id]
        if record is None:
            continue
        candidates.append(
            {
                "mal_id": record.provider_id,
                "title": record.canonical_title,
                "primary": work.is_primary,
                "source": work.source,
                "confidence": work.confidence,
                "relation_distance": work.relation_distance,
                "order": work.display_order,
            }
        )
    return {
        "collection": entry.relative_path,
        "primary_mal_id": (
            None if primary_record is None else primary_record.provider_id
        ),
        "candidates": candidates,
        "manual_rules": {
            "candidate_mal_ids": list(sidecar.manual_candidate_ids),
            "work_rules": [
                {
                    "name": rule.name,
                    "mal_id": rule.mal_id,
                    "selectors": sum(
                        (
                            bool(rule.files),
                            rule.season is not None,
                            rule.local_episode_start is not None,
                        )
                    ),
                    "order": rule.order,
                }
                for rule in sidecar.works
            ],
            "exact_overrides": len(sidecar.media),
            "status": "invalid" if sidecar_error else "valid",
            "error": sidecar_error,
        },
        "files": files,
        "files_total": len(media),
        "files_truncated": truncated,
    }


def validate_collection_sidecar(
    repository: CatalogueRepository,
    media_root: Path,
    collection: str,
) -> dict[str, object]:
    """Parse and validate a collection sidecar without changing state."""

    entry = _entry(repository, collection)
    sidecar, error = _sidecar(media_root, entry, repository)
    if error is not None:
        raise ValueError(error)
    return {
        "collection": entry.relative_path,
        "status": "valid",
        "candidate_mal_ids": list(sidecar.manual_candidate_ids),
        "work_rules": len(sidecar.works),
        "exact_overrides": len(sidecar.media),
    }


def refresh_candidates(
    repository: CatalogueRepository,
    collection: str,
    *,
    verify_work: WorkVerifier | None,
    refreshed_at: datetime | None = None,
) -> dict[str, object]:
    """Re-evaluate the bounded relation graph for one collection."""

    entry = _entry(repository, collection)
    has_manual = any(
        work.source == "manual"
        for work in repository.list_library_entry_works(entry.id)
    )
    result = discover_related_candidates(
        repository,
        entry,
        [item.filename for item in repository.list_media_files(entry.id)],
        has_manual_candidates=has_manual,
        verified_at=refreshed_at or datetime.now(UTC),
        verify_work=verify_work,
    )
    return {
        "collection": entry.relative_path,
        "associated": result.associated,
        "expanded": result.expanded,
        "reasons": list(result.reasons),
        "errors": [_bounded(item, 200) for item in result.errors[:12]],
    }


def invalidate_model(
    repository: CatalogueRepository, collection: str
) -> dict[str, object]:
    entry = _entry(repository, collection)
    mappings, caches = repository.invalidate_model_mappings(entry.id)
    return {
        "collection": entry.relative_path,
        "model_mappings_removed": mappings,
        "model_cache_entries_removed": caches,
    }


def recompute_deterministic(
    repository: CatalogueRepository,
    collection: str,
    *,
    mapped_at: datetime | None = None,
) -> dict[str, object]:
    entry = _entry(repository, collection)
    result = map_entry_deterministically(
        repository,
        entry.id,
        mapped_at=mapped_at or datetime.now(UTC),
        rules_digest=repository.get_mapping_rules_digest(entry.id) or "",
    )
    outcomes: dict[str, int] = {}
    for decision in result.decisions:
        outcomes[decision.outcome] = outcomes.get(decision.outcome, 0) + 1
    return {
        "collection": entry.relative_path,
        "outcomes": dict(sorted(outcomes.items())),
    }


def _entry(repository: CatalogueRepository, collection: str) -> LibraryEntry:
    value = collection.strip().strip("/")
    entry = repository.get_library_entry(value)
    if entry is None:
        raise CollectionNotFoundError(f"unknown collection {value!r}")
    return entry


def _sidecar(
    media_root: Path,
    entry: LibraryEntry,
    repository: CatalogueRepository,
) -> tuple[Sidecar, str | None]:
    try:
        sidecar = read_sidecar(media_root / entry.relative_path / SIDECAR_NAME)
        validate_local_files(
            sidecar,
            {item.filename for item in repository.list_media_files(entry.id)},
        )
    except (OSError, ValueError) as error:
        return Sidecar(), _bounded(error, 240)
    return sidecar, None


def _bounded(value: object, limit: int) -> str:
    text = "".join(
        character if character.isprintable() else " " for character in str(value)
    )
    return text[:limit]
