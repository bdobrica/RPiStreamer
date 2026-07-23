"""Read-only media discovery and transactional catalogue reconciliation."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rpi_streamer.config import parse_bool
from rpi_streamer.database import CatalogueRepository, ScanRun

SIDECAR_NAME: Final = "rpi-streamer.ini"
SIDECAR_SECTION: Final = "rpi-streamer"
SUPPORTED_EXTENSIONS: Final = frozenset({".mp4"})

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
_SIDECAR_KEYS: Final = frozenset(
    {"display_title", "sort_title", "metadata_enabled", "mal_id"}
)


@dataclass(frozen=True, slots=True)
class ScanIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class Sidecar:
    display_title: str | None = None
    sort_title: str | None = None
    metadata_enabled: bool = True
    mal_id: str | None = None


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
            )
        )

    titles.sort(key=lambda item: (natural_key(item.sort_title), item.relative_path))
    return Discovery(tuple(titles), tuple(issues))


def scan_library(
    repository: CatalogueRepository,
    media_root: Path,
    *,
    scanned_at: datetime | None = None,
) -> ScanRun:
    """Discover and reconcile one scan, recording success or partial status."""

    timestamp = datetime.now(UTC) if scanned_at is None else scanned_at
    run = repository.start_scan(started_at=timestamp)
    try:
        discovery = discover(media_root)
        with repository.transaction():
            for title in discovery.titles:
                _relocate_title_if_matched(repository, title)
                entry = repository.upsert_library_entry(
                    relative_path=title.relative_path,
                    title=title.title,
                    sort_title=title.sort_title,
                    seen_at=timestamp,
                    metadata_enabled=title.metadata_enabled,
                    pinned_provider=title.pinned_provider,
                    pinned_provider_id=title.pinned_provider_id,
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
        status = "partial" if discovery.issues else "success"
        summary = _summary(discovery.issues)
        return repository.finish_scan(
            run.id,
            status=status,
            discovered_entries=len(discovery.titles),
            discovered_files=sum(len(title.files) for title in discovery.titles),
            error_count=len(discovery.issues),
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


def _read_sidecar(path: Path, root: Path) -> tuple[Sidecar, ScanIssue | None]:
    if not path.exists():
        return Sidecar(), None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        if parser.sections() != [SIDECAR_SECTION]:
            raise ValueError(f"must contain only [{SIDECAR_SECTION}]")
        values = dict(parser[SIDECAR_SECTION])
        unknown = sorted(set(values) - _SIDECAR_KEYS)
        if unknown:
            raise ValueError(f"unknown option(s): {', '.join(unknown)}")
        display_title = _optional_text(values.get("display_title"))
        sort_title = _optional_text(values.get("sort_title"))
        metadata_enabled = parse_bool(
            values.get("metadata_enabled", "true"), name="metadata_enabled"
        )
        mal_id = _optional_text(values.get("mal_id"))
        if mal_id is not None and not mal_id.isdecimal():
            raise ValueError("mal_id must be a positive integer")
        if mal_id is not None and int(mal_id) <= 0:
            raise ValueError("mal_id must be a positive integer")
        return Sidecar(display_title, sort_title, metadata_enabled, mal_id), None
    except (OSError, UnicodeError, configparser.Error, ValueError) as error:
        relative = path.relative_to(root).as_posix()
        return Sidecar(), ScanIssue(relative, f"invalid sidecar: {error}")


def _derive_title(folder_name: str) -> str:
    derived = re.sub(r"[._]+", " ", folder_name)
    return " ".join(derived.split()) or folder_name


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("title override must not be empty")
    return cleaned


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _summary(issues: tuple[ScanIssue, ...]) -> str | None:
    if not issues:
        return None
    return "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
