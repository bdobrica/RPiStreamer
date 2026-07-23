"""Versioned SQLite persistence for the RPi Streamer catalogue."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

LATEST_SCHEMA_VERSION: Final = 1
BUSY_TIMEOUT_MS: Final = 5000


class DatabaseError(RuntimeError):
    """Base error raised by the persistence layer."""


class UnsupportedSchemaError(DatabaseError):
    """Raised when a database was created by a newer application version."""


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    id: int
    relative_path: str
    title: str
    sort_title: str
    available: bool
    metadata_enabled: bool
    pinned_provider: str | None
    pinned_provider_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class MediaFile:
    id: int
    library_entry_id: int
    relative_path: str
    filename: str
    size_bytes: int
    mtime_ns: int
    episode_hint: str | None
    available: bool
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    id: int
    library_entry_id: int
    provider: str
    provider_id: str
    canonical_title: str
    synopsis: str | None
    episode_count: int | None
    raw_json: str
    etag: str | None
    last_modified: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Relation:
    relation_type: str
    target_provider: str
    target_provider_id: str
    target_title: str


@dataclass(frozen=True, slots=True)
class Artwork:
    id: int
    provider_record_id: int
    kind: str
    source_url: str
    relative_path: str | None
    mime_type: str | None
    size_bytes: int | None
    etag: str | None
    last_modified: str | None
    fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScanRun:
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    discovered_entries: int
    discovered_files: int
    error_count: int
    summary: str | None


_MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {
    1: (
        """
        CREATE TABLE library_entries (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL CHECK (length(title) > 0),
            sort_title TEXT NOT NULL CHECK (length(sort_title) > 0),
            available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
            metadata_enabled INTEGER NOT NULL DEFAULT 1
                CHECK (metadata_enabled IN (0, 1)),
            pinned_provider TEXT,
            pinned_provider_id TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            CHECK (
                (pinned_provider IS NULL AND pinned_provider_id IS NULL)
                OR
                (pinned_provider IS NOT NULL AND pinned_provider_id IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE media_files (
            id INTEGER PRIMARY KEY,
            library_entry_id INTEGER NOT NULL
                REFERENCES library_entries(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL CHECK (length(filename) > 0),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
            episode_hint TEXT,
            available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX media_files_entry_available_idx
        ON media_files(library_entry_id, available, relative_path)
        """,
        """
        CREATE TABLE provider_records (
            id INTEGER PRIMARY KEY,
            library_entry_id INTEGER NOT NULL
                REFERENCES library_entries(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            canonical_title TEXT NOT NULL CHECK (length(canonical_title) > 0),
            synopsis TEXT,
            episode_count INTEGER CHECK (
                episode_count IS NULL OR episode_count >= 0
            ),
            raw_json TEXT NOT NULL,
            etag TEXT,
            last_modified TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE (library_entry_id, provider),
            UNIQUE (provider, provider_id)
        )
        """,
        """
        CREATE INDEX provider_records_fetched_idx
        ON provider_records(provider, fetched_at)
        """,
        """
        CREATE TABLE aliases (
            id INTEGER PRIMARY KEY,
            provider_record_id INTEGER NOT NULL
                REFERENCES provider_records(id) ON DELETE CASCADE,
            alias_type TEXT NOT NULL,
            title TEXT NOT NULL CHECK (length(title) > 0),
            UNIQUE (provider_record_id, alias_type, title)
        )
        """,
        """
        CREATE TABLE genres (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE
                CHECK (length(name) > 0)
        )
        """,
        """
        CREATE TABLE provider_record_genres (
            provider_record_id INTEGER NOT NULL
                REFERENCES provider_records(id) ON DELETE CASCADE,
            genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
            PRIMARY KEY (provider_record_id, genre_id)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY,
            source_provider_record_id INTEGER NOT NULL
                REFERENCES provider_records(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            target_provider_id TEXT NOT NULL,
            target_title TEXT NOT NULL,
            UNIQUE (
                source_provider_record_id,
                relation_type,
                target_provider,
                target_provider_id
            )
        )
        """,
        """
        CREATE TABLE artwork (
            id INTEGER PRIMARY KEY,
            provider_record_id INTEGER NOT NULL
                REFERENCES provider_records(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            source_url TEXT NOT NULL,
            relative_path TEXT,
            mime_type TEXT,
            size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
            etag TEXT,
            last_modified TEXT,
            fetched_at TEXT,
            UNIQUE (provider_record_id, kind)
        )
        """,
        """
        CREATE TABLE scan_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'success', 'partial', 'failed')
            ),
            discovered_entries INTEGER NOT NULL DEFAULT 0
                CHECK (discovered_entries >= 0),
            discovered_files INTEGER NOT NULL DEFAULT 0
                CHECK (discovered_files >= 0),
            error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
            summary TEXT,
            CHECK (
                (status = 'running' AND finished_at IS NULL)
                OR
                (status != 'running' AND finished_at IS NOT NULL)
            )
        )
        """,
        """
        CREATE INDEX scan_runs_started_idx ON scan_runs(started_at DESC)
        """,
    )
}


class CatalogueRepository:
    """Own a SQLite connection and provide catalogue-specific operations."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        path = str(database_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self._connection.row_factory = sqlite3.Row
        self._transaction_depth = 0
        self._closed = False
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            self.journal_mode = self._enable_wal()
            self.migrate()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> CatalogueRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    @property
    def foreign_keys_enabled(self) -> bool:
        row = self._connection.execute("PRAGMA foreign_keys").fetchone()
        return bool(row[0])

    @property
    def busy_timeout_ms(self) -> int:
        row = self._connection.execute("PRAGMA busy_timeout").fetchone()
        return int(row[0])

    def close(self) -> None:
        if not self._closed:
            if self._transaction_depth:
                self._connection.rollback()
            self._connection.close()
            self._closed = True

    def migrate(self) -> None:
        """Apply every pending forward migration in one transaction."""

        with self.transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    applied_at TEXT NOT NULL
                )
                """
            )
            current = self.schema_version
            if current > LATEST_SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"database schema {current} is newer than supported "
                    f"version {LATEST_SCHEMA_VERSION}"
                )
            for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise DatabaseError(f"missing database migration {version}")
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (version, _utc_text()),
                )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Open a transaction, using savepoints when calls are nested."""

        depth = self._transaction_depth
        savepoint = f"rpi_streamer_{depth}"
        if depth == 0:
            self._connection.execute("BEGIN IMMEDIATE")
        else:
            self._connection.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if depth == 0:
                self._connection.rollback()
            else:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self._transaction_depth -= 1
            if depth == 0:
                self._connection.commit()
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def upsert_library_entry(
        self,
        *,
        relative_path: str,
        title: str,
        sort_title: str | None = None,
        seen_at: datetime | None = None,
        metadata_enabled: bool = True,
        pinned_provider: str | None = None,
        pinned_provider_id: str | None = None,
    ) -> LibraryEntry:
        path = canonical_relative_path(relative_path)
        clean_title = _required_text(title, "title")
        clean_sort_title = _required_text(sort_title or title, "sort_title")
        timestamp = _utc_text(seen_at)
        if (pinned_provider is None) != (pinned_provider_id is None):
            raise ValueError(
                "pinned_provider and pinned_provider_id must be set together"
            )
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO library_entries(
                    relative_path, title, sort_title, available,
                    metadata_enabled, pinned_provider, pinned_provider_id,
                    first_seen_at, last_seen_at
                )
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    title = excluded.title,
                    sort_title = excluded.sort_title,
                    available = 1,
                    metadata_enabled = excluded.metadata_enabled,
                    pinned_provider = excluded.pinned_provider,
                    pinned_provider_id = excluded.pinned_provider_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    path,
                    clean_title,
                    clean_sort_title,
                    int(metadata_enabled),
                    pinned_provider,
                    pinned_provider_id,
                    timestamp,
                    timestamp,
                ),
            )
        entry = self.get_library_entry(path)
        if entry is None:
            raise DatabaseError("library entry disappeared after upsert")
        return entry

    def get_library_entry(self, relative_path: str) -> LibraryEntry | None:
        row = self._connection.execute(
            "SELECT * FROM library_entries WHERE relative_path = ?",
            (canonical_relative_path(relative_path),),
        ).fetchone()
        return None if row is None else _library_entry(row)

    def list_library_entries(
        self, *, available_only: bool = True
    ) -> list[LibraryEntry]:
        where = "WHERE available = 1" if available_only else ""
        rows = self._connection.execute(
            f"""
            SELECT * FROM library_entries
            {where}
            ORDER BY sort_title COLLATE NOCASE, relative_path
            """
        ).fetchall()
        return [_library_entry(row) for row in rows]

    def mark_unseen_entries_unavailable(self, seen_before: datetime) -> int:
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE library_entries
                SET available = 0
                WHERE available = 1 AND last_seen_at < ?
                """,
                (_utc_text(seen_before),),
            )
        return cursor.rowcount

    def upsert_media_file(
        self,
        *,
        library_entry_id: int,
        relative_path: str,
        size_bytes: int,
        mtime_ns: int,
        episode_hint: str | None = None,
        seen_at: datetime | None = None,
    ) -> MediaFile:
        path = canonical_relative_path(relative_path)
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if mtime_ns < 0:
            raise ValueError("mtime_ns must be non-negative")
        timestamp = _utc_text(seen_at)
        filename = PurePosixPath(path).name
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO media_files(
                    library_entry_id, relative_path, filename, size_bytes,
                    mtime_ns, episode_hint, available, first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    library_entry_id = excluded.library_entry_id,
                    filename = excluded.filename,
                    size_bytes = excluded.size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    episode_hint = excluded.episode_hint,
                    available = 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    library_entry_id,
                    path,
                    filename,
                    size_bytes,
                    mtime_ns,
                    episode_hint,
                    timestamp,
                    timestamp,
                ),
            )
        row = self._connection.execute(
            "SELECT * FROM media_files WHERE relative_path = ?", (path,)
        ).fetchone()
        if row is None:
            raise DatabaseError("media file disappeared after upsert")
        return _media_file(row)

    def list_media_files(
        self,
        library_entry_id: int,
        *,
        available_only: bool = True,
    ) -> list[MediaFile]:
        availability = "AND available = 1" if available_only else ""
        rows = self._connection.execute(
            f"""
            SELECT * FROM media_files
            WHERE library_entry_id = ? {availability}
            ORDER BY relative_path
            """,
            (library_entry_id,),
        ).fetchall()
        return [_media_file(row) for row in rows]

    def mark_unseen_media_unavailable(
        self, library_entry_id: int, seen_before: datetime
    ) -> int:
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE media_files
                SET available = 0
                WHERE library_entry_id = ?
                    AND available = 1
                    AND last_seen_at < ?
                """,
                (library_entry_id, _utc_text(seen_before)),
            )
        return cursor.rowcount

    def upsert_provider_record(
        self,
        *,
        library_entry_id: int,
        provider: str,
        provider_id: str,
        canonical_title: str,
        raw_data: object,
        fetched_at: datetime | None = None,
        synopsis: str | None = None,
        episode_count: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> ProviderRecord:
        if episode_count is not None and episode_count < 0:
            raise ValueError("episode_count must be non-negative")
        raw_json = json.dumps(
            raw_data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        values = (
            library_entry_id,
            _required_text(provider, "provider"),
            _required_text(provider_id, "provider_id"),
            _required_text(canonical_title, "canonical_title"),
            synopsis,
            episode_count,
            raw_json,
            etag,
            last_modified,
            _utc_text(fetched_at),
        )
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO provider_records(
                    library_entry_id, provider, provider_id, canonical_title,
                    synopsis, episode_count, raw_json, etag, last_modified,
                    fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_entry_id, provider) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    canonical_title = excluded.canonical_title,
                    synopsis = excluded.synopsis,
                    episode_count = excluded.episode_count,
                    raw_json = excluded.raw_json,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    fetched_at = excluded.fetched_at
                """,
                values,
            )
        record = self.get_provider_record(library_entry_id, provider)
        if record is None:
            raise DatabaseError("provider record disappeared after upsert")
        return record

    def get_provider_record(
        self, library_entry_id: int, provider: str
    ) -> ProviderRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM provider_records
            WHERE library_entry_id = ? AND provider = ?
            """,
            (library_entry_id, provider),
        ).fetchone()
        return None if row is None else _provider_record(row)

    def list_stale_provider_records(
        self, provider: str, stale_before: datetime
    ) -> list[ProviderRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM provider_records
            WHERE provider = ? AND fetched_at < ?
            ORDER BY fetched_at, id
            """,
            (provider, _utc_text(stale_before)),
        ).fetchall()
        return [_provider_record(row) for row in rows]

    def replace_aliases(
        self, provider_record_id: int, aliases: Sequence[tuple[str, str]]
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "DELETE FROM aliases WHERE provider_record_id = ?",
                (provider_record_id,),
            )
            self._connection.executemany(
                """
                INSERT INTO aliases(provider_record_id, alias_type, title)
                VALUES (?, ?, ?)
                """,
                (
                    (
                        provider_record_id,
                        _required_text(alias_type, "alias_type"),
                        _required_text(title, "alias title"),
                    )
                    for alias_type, title in aliases
                ),
            )

    def list_aliases(self, provider_record_id: int) -> list[tuple[str, str]]:
        rows = self._connection.execute(
            """
            SELECT alias_type, title FROM aliases
            WHERE provider_record_id = ?
            ORDER BY alias_type, title
            """,
            (provider_record_id,),
        ).fetchall()
        return [(str(row["alias_type"]), str(row["title"])) for row in rows]

    def replace_genres(self, provider_record_id: int, genres: Sequence[str]) -> None:
        by_casefolded_name: dict[str, str] = {}
        for genre in genres:
            cleaned_genre = _required_text(genre, "genre")
            by_casefolded_name.setdefault(cleaned_genre.casefold(), cleaned_genre)
        cleaned = sorted(by_casefolded_name.values(), key=str.casefold)
        with self.transaction():
            self._connection.execute(
                "DELETE FROM provider_record_genres WHERE provider_record_id = ?",
                (provider_record_id,),
            )
            for genre in cleaned:
                self._connection.execute(
                    "INSERT INTO genres(name) VALUES (?) ON CONFLICT DO NOTHING",
                    (genre,),
                )
                self._connection.execute(
                    """
                    INSERT INTO provider_record_genres(
                        provider_record_id, genre_id
                    )
                    SELECT ?, id FROM genres WHERE name = ? COLLATE NOCASE
                    """,
                    (provider_record_id, genre),
                )

    def list_genres(self, provider_record_id: int) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT genres.name
            FROM genres
            JOIN provider_record_genres
                ON provider_record_genres.genre_id = genres.id
            WHERE provider_record_genres.provider_record_id = ?
            ORDER BY genres.name COLLATE NOCASE
            """,
            (provider_record_id,),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def replace_relations(
        self, provider_record_id: int, relations: Sequence[Relation]
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "DELETE FROM relations WHERE source_provider_record_id = ?",
                (provider_record_id,),
            )
            self._connection.executemany(
                """
                INSERT INTO relations(
                    source_provider_record_id, relation_type, target_provider,
                    target_provider_id, target_title
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        provider_record_id,
                        _required_text(relation.relation_type, "relation_type"),
                        _required_text(relation.target_provider, "target_provider"),
                        _required_text(
                            relation.target_provider_id, "target_provider_id"
                        ),
                        _required_text(relation.target_title, "target_title"),
                    )
                    for relation in relations
                ),
            )

    def list_relations(self, provider_record_id: int) -> list[Relation]:
        rows = self._connection.execute(
            """
            SELECT relation_type, target_provider, target_provider_id,
                target_title
            FROM relations
            WHERE source_provider_record_id = ?
            ORDER BY relation_type, target_title
            """,
            (provider_record_id,),
        ).fetchall()
        return [
            Relation(
                relation_type=str(row["relation_type"]),
                target_provider=str(row["target_provider"]),
                target_provider_id=str(row["target_provider_id"]),
                target_title=str(row["target_title"]),
            )
            for row in rows
        ]

    def upsert_artwork(
        self,
        *,
        provider_record_id: int,
        kind: str,
        source_url: str,
        relative_path: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        fetched_at: datetime | None = None,
    ) -> Artwork:
        if size_bytes is not None and size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        stored_path = (
            canonical_relative_path(relative_path)
            if relative_path is not None
            else None
        )
        timestamp = _utc_text(fetched_at) if fetched_at is not None else None
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO artwork(
                    provider_record_id, kind, source_url, relative_path,
                    mime_type, size_bytes, etag, last_modified, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_record_id, kind) DO UPDATE SET
                    source_url = excluded.source_url,
                    relative_path = excluded.relative_path,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    fetched_at = excluded.fetched_at
                """,
                (
                    provider_record_id,
                    _required_text(kind, "artwork kind"),
                    _required_text(source_url, "source_url"),
                    stored_path,
                    mime_type,
                    size_bytes,
                    etag,
                    last_modified,
                    timestamp,
                ),
            )
        row = self._connection.execute(
            """
            SELECT * FROM artwork
            WHERE provider_record_id = ? AND kind = ?
            """,
            (provider_record_id, kind),
        ).fetchone()
        if row is None:
            raise DatabaseError("artwork disappeared after upsert")
        return _artwork(row)

    def start_scan(self, *, started_at: datetime | None = None) -> ScanRun:
        timestamp = _utc_text(started_at)
        with self.transaction():
            cursor = self._connection.execute(
                "INSERT INTO scan_runs(started_at, status) VALUES (?, 'running')",
                (timestamp,),
            )
        scan_run_id = cursor.lastrowid
        if scan_run_id is None:
            raise DatabaseError("scan run insert did not return an identifier")
        return self.get_scan_run(scan_run_id)

    def finish_scan(
        self,
        scan_run_id: int,
        *,
        status: str,
        discovered_entries: int,
        discovered_files: int,
        error_count: int = 0,
        summary: str | None = None,
        finished_at: datetime | None = None,
    ) -> ScanRun:
        if status not in {"success", "partial", "failed"}:
            raise ValueError("finished scan status must be success, partial, or failed")
        if min(discovered_entries, discovered_files, error_count) < 0:
            raise ValueError("scan counts must be non-negative")
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, status = ?, discovered_entries = ?,
                    discovered_files = ?, error_count = ?, summary = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    _utc_text(finished_at),
                    status,
                    discovered_entries,
                    discovered_files,
                    error_count,
                    summary,
                    scan_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DatabaseError(
                    f"scan run {scan_run_id} is missing or already finished"
                )
        return self.get_scan_run(scan_run_id)

    def get_scan_run(self, scan_run_id: int) -> ScanRun:
        row = self._connection.execute(
            "SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError(f"scan run {scan_run_id} not found")
        return _scan_run(row)

    def _enable_wal(self) -> str:
        try:
            row = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError:
            row = self._connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        return str(row[0]).lower()


def canonical_relative_path(value: str) -> str:
    """Validate and normalize a stored POSIX path relative to media/state root."""

    if not value or "\x00" in value or "\\" in value:
        raise ValueError("relative path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"path must be canonical and relative: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError(f"path must be canonical and relative: {value!r}")
    return normalized


def _required_text(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    return cleaned


def _utc_text(value: datetime | None = None) -> str:
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _library_entry(row: sqlite3.Row) -> LibraryEntry:
    first_seen = _datetime(str(row["first_seen_at"]))
    last_seen = _datetime(str(row["last_seen_at"]))
    assert first_seen is not None and last_seen is not None
    return LibraryEntry(
        id=int(row["id"]),
        relative_path=str(row["relative_path"]),
        title=str(row["title"]),
        sort_title=str(row["sort_title"]),
        available=bool(row["available"]),
        metadata_enabled=bool(row["metadata_enabled"]),
        pinned_provider=(
            None if row["pinned_provider"] is None else str(row["pinned_provider"])
        ),
        pinned_provider_id=(
            None
            if row["pinned_provider_id"] is None
            else str(row["pinned_provider_id"])
        ),
        first_seen_at=first_seen,
        last_seen_at=last_seen,
    )


def _media_file(row: sqlite3.Row) -> MediaFile:
    first_seen = _datetime(str(row["first_seen_at"]))
    last_seen = _datetime(str(row["last_seen_at"]))
    assert first_seen is not None and last_seen is not None
    return MediaFile(
        id=int(row["id"]),
        library_entry_id=int(row["library_entry_id"]),
        relative_path=str(row["relative_path"]),
        filename=str(row["filename"]),
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"]),
        episode_hint=(
            None if row["episode_hint"] is None else str(row["episode_hint"])
        ),
        available=bool(row["available"]),
        first_seen_at=first_seen,
        last_seen_at=last_seen,
    )


def _provider_record(row: sqlite3.Row) -> ProviderRecord:
    fetched_at = _datetime(str(row["fetched_at"]))
    assert fetched_at is not None
    return ProviderRecord(
        id=int(row["id"]),
        library_entry_id=int(row["library_entry_id"]),
        provider=str(row["provider"]),
        provider_id=str(row["provider_id"]),
        canonical_title=str(row["canonical_title"]),
        synopsis=None if row["synopsis"] is None else str(row["synopsis"]),
        episode_count=(
            None if row["episode_count"] is None else int(row["episode_count"])
        ),
        raw_json=str(row["raw_json"]),
        etag=None if row["etag"] is None else str(row["etag"]),
        last_modified=(
            None if row["last_modified"] is None else str(row["last_modified"])
        ),
        fetched_at=fetched_at,
    )


def _artwork(row: sqlite3.Row) -> Artwork:
    return Artwork(
        id=int(row["id"]),
        provider_record_id=int(row["provider_record_id"]),
        kind=str(row["kind"]),
        source_url=str(row["source_url"]),
        relative_path=(
            None if row["relative_path"] is None else str(row["relative_path"])
        ),
        mime_type=None if row["mime_type"] is None else str(row["mime_type"]),
        size_bytes=(None if row["size_bytes"] is None else int(row["size_bytes"])),
        etag=None if row["etag"] is None else str(row["etag"]),
        last_modified=(
            None if row["last_modified"] is None else str(row["last_modified"])
        ),
        fetched_at=_datetime(
            None if row["fetched_at"] is None else str(row["fetched_at"])
        ),
    )


def _scan_run(row: sqlite3.Row) -> ScanRun:
    started_at = _datetime(str(row["started_at"]))
    assert started_at is not None
    return ScanRun(
        id=int(row["id"]),
        started_at=started_at,
        finished_at=_datetime(
            None if row["finished_at"] is None else str(row["finished_at"])
        ),
        status=str(row["status"]),
        discovered_entries=int(row["discovered_entries"]),
        discovered_files=int(row["discovered_files"]),
        error_count=int(row["error_count"]),
        summary=None if row["summary"] is None else str(row["summary"]),
    )
