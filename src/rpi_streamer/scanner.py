"""Read-only media discovery and transactional catalogue reconciliation."""

from __future__ import annotations

import configparser
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rpi_streamer.candidates import discover_related_candidates
from rpi_streamer.database import CatalogueRepository, ProviderRecord, ScanRun
from rpi_streamer.mapping import map_entry_deterministically, parse_local_media_facts
from rpi_streamer.sidecar import (
    MediaOverride,
    Sidecar,
    WorkRule,
    read_sidecar,
    validate_local_files,
)

SIDECAR_NAME: Final = "rpi-streamer.ini"
SIDECAR_SECTION: Final = "rpi-streamer"
SUPPORTED_EXTENSIONS: Final = frozenset({".mp4"})
Enricher = Callable[[CatalogueRepository, datetime], Sequence[str]]
WorkVerifier = Callable[
    [CatalogueRepository, str, datetime], tuple[ProviderRecord | None, str | None]
]

_NATURAL_PART_RE: Final = re.compile(r"(\d+)")
_SEASON_EPISODE_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,3})[ ._-]*E(\d{1,4})(?:[ ._-]*[-~][ ._-]*(\d{1,4}))?",
    re.IGNORECASE,
)
_SPECIAL_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(OVA|OAD|ONA|SPECIAL|SP)(?:[ ._-]*(\d{1,3}))?",
    re.IGNORECASE,
)
_LEADING_EPISODE_RE: Final = re.compile(
    r"^\s*(?:EP(?:ISODE)?[ ._-]*)?(\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?"
    r"(?=$|[ ._-])",
    re.IGNORECASE,
)
_ANY_EPISODE_RE: Final = re.compile(
    r"(?:^|[ ._-])(\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?(?=$|[ ._-])"
)
_ORDINAL_SEASON_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,3})(?:st|nd|rd|th)[ ._-]*season",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScanIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    relative_path: str
    filename: str
    size_bytes: int
    mtime_ns: int
    local_identity: str
    episode_hint: str | None


@dataclass(frozen=True, slots=True)
class DiscoveredTitle:
    relative_path: str
    title: str
    sort_title: str
    metadata_enabled: bool
    pinned_provider: str | None
    pinned_provider_id: str | None
    files: tuple[DiscoveredFile, ...]
    sidecar: Sidecar
    sidecar_valid: bool


@dataclass(frozen=True, slots=True)
class Discovery:
    titles: tuple[DiscoveredTitle, ...]
    issues: tuple[ScanIssue, ...]


def natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a deterministic, case-insensitive natural-sort key."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART_RE.split(value)
        if part
    )


def episode_hint(filename: str) -> str | None:
    """Extract a conservative display hint while keeping filename authoritative."""

    stem = Path(filename).stem
    match = _SEASON_EPISODE_RE.search(stem)
    if match:
        season, first, last = match.groups()
        prefix = f"S{int(season):02d}E{int(first):02d}"
        return prefix if last is None else f"{prefix}-E{int(last):02d}"
    match = _SPECIAL_RE.search(stem)
    if match:
        kind, number = match.groups()
        label = "Special" if kind.casefold() in {"special", "sp"} else kind.upper()
        return label if number is None else f"{label} {int(number)}"
    match = _LEADING_EPISODE_RE.search(stem)
    if match:
        first, last = match.groups()
        return first if last is None else f"{first}-{last}"
    return None


def discover(media_root: Path) -> Discovery:
    """Inspect a media tree without following directory symlinks or writing it."""

    root = media_root.resolve()
    issues: list[ScanIssue] = []
    titles: list[DiscoveredTitle] = []
    seen_identities: set[str] = set()

    def walk_error(error: OSError) -> None:
        path = error.filename or str(root)
        issues.append(ScanIssue(_display_path(root, Path(path)), str(error)))

    for directory, directory_names, filenames in os.walk(
        root, topdown=True, onerror=walk_error, followlinks=False
    ):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            (
                name
                for name in directory_names
                if not (directory_path / name).is_symlink()
                and not (directory_path == root and name == "lost+found")
            ),
            key=natural_key,
        )
        candidates = sorted(
            (
                name
                for name in filenames
                if Path(name).suffix.casefold() in SUPPORTED_EXTENSIONS
            ),
            key=natural_key,
        )
        if not candidates:
            continue

        relative_directory = directory_path.relative_to(root).as_posix()
        if relative_directory == ".":
            issues.append(
                ScanIssue(".", "MP4 files at the media root have no title folder")
            )
            continue

        sidecar, sidecar_issue = _read_sidecar(directory_path / SIDECAR_NAME, root)
        if sidecar_issue is not None:
            issues.append(sidecar_issue)
        files: list[DiscoveredFile] = []
        for filename in candidates:
            path = directory_path / filename
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    issues.append(
                        ScanIssue(
                            path.relative_to(root).as_posix(),
                            "symlink target escapes media_root",
                        )
                    )
                    continue
                stat = path.stat()
                if not resolved.is_file():
                    continue
            except (OSError, RuntimeError) as error:
                issues.append(ScanIssue(path.relative_to(root).as_posix(), str(error)))
                continue
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if identity in seen_identities:
                issues.append(
                    ScanIssue(
                        path.relative_to(root).as_posix(),
                        "duplicate filesystem identity was already discovered",
                    )
                )
                continue
            seen_identities.add(identity)
            relative_path = path.relative_to(root).as_posix()
            files.append(
                DiscoveredFile(
                    relative_path=relative_path,
                    filename=filename,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    local_identity=identity,
                    episode_hint=episode_hint(filename),
                )
            )
        if not files:
            continue
        try:
            validate_local_files(sidecar, {item.filename for item in files})
        except ValueError as error:
            sidecar_issue = ScanIssue(
                (directory_path / SIDECAR_NAME).relative_to(root).as_posix(),
                f"invalid sidecar: {error}",
            )
            issues.append(sidecar_issue)
            sidecar = Sidecar()
        derived_title = _derive_title(directory_path.name)
        title = sidecar.display_title or derived_title
        sort_title = sidecar.sort_title or title
        titles.append(
            DiscoveredTitle(
                relative_path=relative_directory,
                title=title,
                sort_title=sort_title,
                metadata_enabled=sidecar.metadata_enabled,
                pinned_provider="jikan" if sidecar.mal_id is not None else None,
                pinned_provider_id=sidecar.mal_id,
                files=tuple(files),
                sidecar=sidecar,
                sidecar_valid=sidecar_issue is None,
            )
        )

    titles.sort(key=lambda item: (natural_key(item.sort_title), item.relative_path))
    return Discovery(tuple(titles), tuple(issues))


def scan_library(
    repository: CatalogueRepository,
    media_root: Path,
    *,
    scanned_at: datetime | None = None,
    enrich: Enricher | None = None,
    verify_work: WorkVerifier | None = None,
) -> ScanRun:
    """Discover and reconcile one scan, recording success or partial status."""

    timestamp = datetime.now(UTC) if scanned_at is None else scanned_at
    run = repository.start_scan(started_at=timestamp)
    try:
        discovery = discover(media_root)
        with repository.transaction():
            for title in discovery.titles:
                _relocate_title_if_matched(repository, title)
                previous = repository.get_library_entry(title.relative_path)
                if not title.sidecar_valid and previous is not None:
                    title_value = previous.title
                    sort_title = previous.sort_title
                    metadata_enabled = previous.metadata_enabled
                    pinned_provider = previous.pinned_provider
                    pinned_provider_id = previous.pinned_provider_id
                else:
                    title_value = title.title
                    sort_title = title.sort_title
                    metadata_enabled = title.metadata_enabled
                    pinned_provider = title.pinned_provider
                    pinned_provider_id = title.pinned_provider_id
                entry = repository.upsert_library_entry(
                    relative_path=title.relative_path,
                    title=title_value,
                    sort_title=sort_title,
                    seen_at=timestamp,
                    metadata_enabled=metadata_enabled,
                    pinned_provider=pinned_provider,
                    pinned_provider_id=pinned_provider_id,
                )
                for media in title.files:
                    existing = repository.get_media_file_by_identity(
                        media.local_identity
                    )
                    if (
                        existing is not None
                        and existing.relative_path != media.relative_path
                    ):
                        repository.relocate_media_file(
                            existing.id,
                            library_entry_id=entry.id,
                            relative_path=media.relative_path,
                        )
                    repository.upsert_media_file(
                        library_entry_id=entry.id,
                        relative_path=media.relative_path,
                        size_bytes=media.size_bytes,
                        mtime_ns=media.mtime_ns,
                        local_identity=media.local_identity,
                        episode_hint=media.episode_hint,
                        seen_at=timestamp,
                    )
            if not discovery.issues:
                for entry in repository.list_library_entries(available_only=False):
                    repository.mark_unseen_media_unavailable(entry.id, timestamp)
                repository.mark_unseen_entries_unavailable(timestamp)
        metadata_errors = () if enrich is None else tuple(enrich(repository, timestamp))
        mapping_errors = _reconcile_manual_mappings(
            repository,
            discovery.titles,
            timestamp,
            verify_work=verify_work,
        )
        candidate_errors = _discover_relation_candidates(
            repository,
            discovery.titles,
            timestamp,
            verify_work=verify_work,
        )
        deterministic_errors = _reconcile_deterministic_mappings(
            repository, discovery.titles, timestamp
        )
        issues = (
            *discovery.issues,
            *(ScanIssue("metadata", error) for error in metadata_errors),
            *(ScanIssue("mapping", error) for error in mapping_errors),
            *(ScanIssue("candidates", error) for error in candidate_errors),
            *(ScanIssue("mapping", error) for error in deterministic_errors),
        )
        status = "partial" if issues else "success"
        summary = _summary(issues)
        return repository.finish_scan(
            run.id,
            status=status,
            discovered_entries=len(discovery.titles),
            discovered_files=sum(len(title.files) for title in discovery.titles),
            error_count=len(issues),
            summary=summary,
            finished_at=timestamp,
        )
    except Exception as error:
        repository.finish_scan(
            run.id,
            status="failed",
            discovered_entries=0,
            discovered_files=0,
            error_count=1,
            summary=str(error),
            finished_at=timestamp,
        )
        raise


def _relocate_title_if_matched(
    repository: CatalogueRepository, title: DiscoveredTitle
) -> None:
    if repository.get_library_entry(title.relative_path) is not None:
        return
    matched_ids = {
        media.library_entry_id
        for item in title.files
        if (media := repository.get_media_file_by_identity(item.local_identity))
        is not None
    }
    if len(matched_ids) == 1:
        matched = repository.get_library_entry_by_id(matched_ids.pop())
        if matched is not None and not any(
            candidate.relative_path == title.relative_path
            for candidate in repository.list_library_entries(available_only=False)
        ):
            repository.relocate_library_entry(
                matched.id, relative_path=title.relative_path
            )


def _reconcile_manual_mappings(
    repository: CatalogueRepository,
    titles: Sequence[DiscoveredTitle],
    timestamp: datetime,
    *,
    verify_work: WorkVerifier | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    for title in titles:
        if not title.sidecar_valid:
            continue
        entry = repository.get_library_entry(title.relative_path)
        if entry is None:
            continue
        sidecar = title.sidecar
        records: dict[str, ProviderRecord] = {}
        pending = False
        for provider_id in sidecar.manual_candidate_ids:
            record = repository.get_provider_record_by_provider_id("jikan", provider_id)
            verification_error: str | None = None
            if record is None and verify_work is not None:
                record, verification_error = verify_work(
                    repository, provider_id, timestamp
                )
            if record is None:
                pending = True
                detail = verification_error or "not cached and provider unavailable"
                errors.append(
                    f"{title.relative_path}: MAL ID {provider_id} pending: {detail}"
                )
            else:
                records[provider_id] = record

        work_by_id: dict[str, int] = {}
        retained_record_ids: set[int] = set()
        for provider_id, record in records.items():
            rule = next(
                (item for item in sidecar.works if item.mal_id == provider_id),
                None,
            )
            existing = repository.get_library_entry_work_by_provider_id(
                entry.id, "jikan", provider_id
            )
            is_primary = provider_id == sidecar.mal_id or (
                sidecar.mal_id is None and existing is not None and existing.is_primary
            )
            local_name = (
                rule.name
                if rule is not None
                else (
                    existing.local_name
                    if existing is not None
                    else f"manual-{provider_id}"
                )
            )
            work = repository.associate_library_entry_work(
                library_entry_id=entry.id,
                provider_record_id=record.id,
                local_name=local_name,
                source="manual",
                verified_at=timestamp,
                is_primary=is_primary,
                label=(
                    rule.label
                    if rule is not None
                    else (None if existing is None else existing.label)
                ),
                display_order=(
                    rule.order
                    if rule is not None
                    else (0 if existing is None else existing.display_order)
                ),
            )
            work_by_id[provider_id] = work.id
            retained_record_ids.add(record.id)

        exact_by_file = {item.file: item for item in sidecar.media}
        retained_media_ids: set[int] = set()
        conflict = False
        for discovered in title.files:
            media = repository.get_media_file_by_identity(discovered.local_identity)
            if media is None:
                continue
            override = exact_by_file.get(discovered.filename)
            if override is not None:
                if override.mal_id not in work_by_id:
                    continue
                override_kind = override.kind or (
                    "episode" if override.episode is not None else "unknown"
                )
                if _exceeds_provider_episode_count(
                    records[override.mal_id],
                    override_kind,
                    override.episode_end,
                ):
                    conflict = True
                    errors.append(
                        f"{title.relative_path}: {discovered.filename}: "
                        "episode exceeds verified provider count"
                    )
                    continue
                _apply_exact_mapping(
                    repository,
                    media.id,
                    work_by_id[override.mal_id],
                    override,
                    sidecar.digest,
                    timestamp,
                )
                retained_media_ids.add(media.id)
                continue
            season, episode_start, episode_end = _manual_facts(discovered.filename)
            matches = [
                rule
                for rule in sidecar.works
                if rule.matches(
                    discovered.filename,
                    season=season,
                    episode_start=episode_start,
                )
            ]
            if not matches:
                selectorless = [
                    rule
                    for rule in sidecar.works
                    if rule.mal_id == sidecar.mal_id
                    and not rule.files
                    and rule.season is None
                    and rule.local_episode_start is None
                ]
                if len(sidecar.works) == 1:
                    matches = selectorless
            if len(matches) > 1:
                conflict = True
                errors.append(
                    f"{title.relative_path}: {discovered.filename}: "
                    "multiple manual work rules match"
                )
                current = repository.get_media_work_mapping(media.id)
                if current is not None and current.source.startswith("manual_"):
                    retained_media_ids.add(media.id)
                continue
            if not matches:
                continue
            rule = matches[0]
            work_id = work_by_id.get(rule.mal_id)
            if work_id is None:
                continue
            mapped_start = (
                None if episode_start is None else episode_start + rule.episode_offset
            )
            mapped_end = (
                None if episode_end is None else episode_end + rule.episode_offset
            )
            if (
                mapped_start is not None
                and not 1 <= mapped_start <= 9999
                or mapped_end is not None
                and not 1 <= mapped_end <= 9999
            ):
                conflict = True
                errors.append(
                    f"{title.relative_path}: {discovered.filename}: "
                    "episode offset is outside 1-9999"
                )
                continue
            mapped_kind = rule.kind or (
                "episode" if mapped_start is not None else "unknown"
            )
            if _exceeds_provider_episode_count(
                records[rule.mal_id], mapped_kind, mapped_end
            ):
                conflict = True
                errors.append(
                    f"{title.relative_path}: {discovered.filename}: "
                    "episode exceeds verified provider count"
                )
                continue
            _apply_rule_mapping(
                repository,
                media.id,
                work_id,
                rule,
                discovered.filename,
                mapped_start,
                mapped_end,
                sidecar.digest,
                timestamp,
            )
            retained_media_ids.add(media.id)

        if not pending and not conflict:
            repository.remove_stale_manual_mappings(entry.id, retained_media_ids)
            repository.remove_stale_manual_work_associations(
                entry.id, retained_record_ids
            )
        repository.set_mapping_rules_digest(
            entry.id, sidecar.digest, updated_at=timestamp
        )
    return tuple(errors)


def _discover_relation_candidates(
    repository: CatalogueRepository,
    titles: Sequence[DiscoveredTitle],
    timestamp: datetime,
    *,
    verify_work: WorkVerifier | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    for title in titles:
        if not title.sidecar_valid:
            continue
        entry = repository.get_library_entry(title.relative_path)
        if entry is None:
            continue
        result = discover_related_candidates(
            repository,
            entry,
            [media.filename for media in title.files],
            has_manual_candidates=bool(title.sidecar.manual_candidate_ids),
            verified_at=timestamp,
            verify_work=verify_work,
        )
        errors.extend(result.errors)
    return tuple(errors)


def _reconcile_deterministic_mappings(
    repository: CatalogueRepository,
    titles: Sequence[DiscoveredTitle],
    timestamp: datetime,
) -> tuple[str, ...]:
    errors: list[str] = []
    for title in titles:
        if not title.sidecar_valid:
            continue
        entry = repository.get_library_entry(title.relative_path)
        if entry is None:
            continue
        result = map_entry_deterministically(
            repository,
            entry.id,
            mapped_at=timestamp,
            rules_digest=title.sidecar.digest,
        )
        for decision in result.decisions:
            if decision.outcome in {"ambiguous", "invalid"}:
                errors.append(
                    f"{title.relative_path}: {decision.filename}: "
                    f"{decision.outcome}: {decision.reason}"
                )
    return tuple(errors)


def _exceeds_provider_episode_count(
    record: ProviderRecord, kind: str, episode_end: int | None
) -> bool:
    return (
        kind == "episode"
        and episode_end is not None
        and record.episode_count is not None
        and episode_end > record.episode_count
    )


def _apply_exact_mapping(
    repository: CatalogueRepository,
    media_file_id: int,
    work_id: int,
    override: MediaOverride,
    rules_digest: str,
    timestamp: datetime,
) -> None:
    digest = _mapping_digest(rules_digest, "exact", override.name, override.file)
    kind = override.kind or ("episode" if override.episode is not None else "unknown")
    _set_mapping_if_changed(
        repository,
        media_file_id=media_file_id,
        library_entry_work_id=work_id,
        kind=kind,
        episode_start=override.episode,
        episode_end=override.episode_end,
        label=override.label,
        source="manual_exact",
        input_digest=digest,
        mapped_at=timestamp,
    )


def _apply_rule_mapping(
    repository: CatalogueRepository,
    media_file_id: int,
    work_id: int,
    rule: WorkRule,
    filename: str,
    episode_start: int | None,
    episode_end: int | None,
    rules_digest: str,
    timestamp: datetime,
) -> None:
    digest = _mapping_digest(rules_digest, "work", rule.name, filename)
    kind = rule.kind or ("episode" if episode_start is not None else "unknown")
    _set_mapping_if_changed(
        repository,
        media_file_id=media_file_id,
        library_entry_work_id=work_id,
        kind=kind,
        episode_start=episode_start,
        episode_end=episode_end,
        label=None,
        source="manual_rule",
        input_digest=digest,
        mapped_at=timestamp,
    )


def _set_mapping_if_changed(
    repository: CatalogueRepository,
    *,
    media_file_id: int,
    library_entry_work_id: int,
    kind: str,
    episode_start: int | None,
    episode_end: int | None,
    label: str | None,
    source: str,
    input_digest: str,
    mapped_at: datetime,
) -> None:
    current = repository.get_media_work_mapping(media_file_id)
    if current is not None and all(
        (
            current.library_entry_work_id == library_entry_work_id,
            current.kind == kind,
            current.episode_start == episode_start,
            current.episode_end == episode_end,
            current.label == label,
            current.source == source,
            current.input_digest == input_digest,
        )
    ):
        return
    repository.set_media_work_mapping(
        media_file_id=media_file_id,
        library_entry_work_id=library_entry_work_id,
        kind=kind,
        episode_start=episode_start,
        episode_end=episode_end,
        label=label,
        source=source,
        input_digest=input_digest,
        mapped_at=mapped_at,
    )


def _mapping_digest(*parts: str) -> str:
    import hashlib

    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _manual_facts(filename: str) -> tuple[int | None, int | None, int | None]:
    facts = parse_local_media_facts(filename)
    season = facts.season
    if season is None and facts.episode_start is not None:
        season = 1
    return season, facts.episode_start, facts.episode_end


def _read_sidecar(path: Path, root: Path) -> tuple[Sidecar, ScanIssue | None]:
    try:
        return read_sidecar(path), None
    except (OSError, UnicodeError, configparser.Error, ValueError) as error:
        relative = path.relative_to(root).as_posix()
        return Sidecar(), ScanIssue(relative, f"invalid sidecar: {error}")


def _derive_title(folder_name: str) -> str:
    derived = re.sub(r"[._]+", " ", folder_name)
    return " ".join(derived.split()) or folder_name


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _summary(issues: tuple[ScanIssue, ...]) -> str | None:
    if not issues:
        return None
    return "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
