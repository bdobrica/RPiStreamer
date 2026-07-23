from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rpi_streamer.database import (
    BUSY_TIMEOUT_MS,
    LATEST_SCHEMA_VERSION,
    CatalogueRepository,
    DatabaseError,
    Relation,
    UnsupportedSchemaError,
    canonical_relative_path,
)

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "state" / "catalogue.db"
        )
        self.repository = CatalogueRepository(self.database_path)
        self.addCleanup(self.repository.close)

    def _entry(self, path: str = "Cowboy Bebop") -> int:
        return self.repository.upsert_library_entry(
            relative_path=path,
            title=path,
            seen_at=NOW,
        ).id

    def _provider_record(self, entry_id: int) -> int:
        return self.repository.upsert_provider_record(
            library_entry_id=entry_id,
            provider="jikan",
            provider_id="1",
            canonical_title="Cowboy Bebop",
            synopsis="Bounty hunters in space.",
            episode_count=26,
            raw_data={"mal_id": 1, "title": "Cowboy Bebop"},
            etag='"abc"',
            fetched_at=NOW,
        ).id

    def test_fresh_database_is_migrated_and_configured(self) -> None:
        self.assertEqual(self.repository.schema_version, LATEST_SCHEMA_VERSION)
        self.assertTrue(self.repository.foreign_keys_enabled)
        self.assertEqual(self.repository.busy_timeout_ms, BUSY_TIMEOUT_MS)
        self.assertIn(
            self.repository.journal_mode, {"wal", "delete", "memory", "truncate"}
        )
        self.assertTrue(self.database_path.is_file())

    def test_migrations_are_idempotent(self) -> None:
        self.repository.migrate()
        self.repository.migrate()

        self.assertEqual(self.repository.schema_version, LATEST_SCHEMA_VERSION)

    def test_newer_schema_is_rejected(self) -> None:
        path = Path(self.temporary_directory.name) / "future.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (999, '2026-01-01T00:00:00Z');
            """
        )
        connection.close()

        with self.assertRaisesRegex(UnsupportedSchemaError, "newer"):
            CatalogueRepository(path)

    def test_library_and_media_crud_and_reconciliation(self) -> None:
        entry = self.repository.upsert_library_entry(
            relative_path="Cowboy Bebop",
            title="Cowboy Bebop",
            sort_title="Bebop, Cowboy",
            seen_at=NOW,
            pinned_provider="jikan",
            pinned_provider_id="1",
        )
        media = self.repository.upsert_media_file(
            library_entry_id=entry.id,
            relative_path="Cowboy Bebop/01 - Asteroid Blues.mp4",
            size_bytes=123456,
            mtime_ns=987654321,
            local_identity="1:234",
            episode_hint="1",
            seen_at=NOW,
        )

        self.assertEqual(media.filename, "01 - Asteroid Blues.mp4")
        self.assertEqual(media.local_identity, "1:234")
        self.assertEqual(self.repository.list_media_files(entry.id), [media])
        updated = self.repository.upsert_media_file(
            library_entry_id=entry.id,
            relative_path=media.relative_path,
            size_bytes=222222,
            mtime_ns=999999999,
            episode_hint="01",
            seen_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(updated.id, media.id)
        self.assertEqual(updated.size_bytes, 222222)
        self.repository.upsert_library_entry(
            relative_path=entry.relative_path,
            title=entry.title,
            sort_title=entry.sort_title,
            seen_at=NOW + timedelta(minutes=2),
            pinned_provider="jikan",
            pinned_provider_id="1",
        )

        cutoff = NOW + timedelta(minutes=1)
        self.assertEqual(
            self.repository.mark_unseen_media_unavailable(entry.id, cutoff), 0
        )
        older_entry = self._entry("Older Show")
        self.assertEqual(self.repository.mark_unseen_entries_unavailable(cutoff), 1)
        self.assertFalse(
            self.repository.get_library_entry("Older Show").available  # type: ignore[union-attr]
        )
        self.assertNotEqual(older_entry, entry.id)

    def test_outer_transaction_rolls_back_nested_repository_writes(self) -> None:
        with (
            self.assertRaisesRegex(RuntimeError, "cancel scan"),
            self.repository.transaction(),
        ):
            self._entry()
            raise RuntimeError("cancel scan")

        self.assertIsNone(self.repository.get_library_entry("Cowboy Bebop"))

    def test_constraints_and_input_validation(self) -> None:
        entry_id = self._entry()
        cases = (
            lambda: self.repository.upsert_media_file(
                library_entry_id=9999,
                relative_path="Cowboy Bebop/01.mp4",
                size_bytes=1,
                mtime_ns=1,
            ),
            lambda: self.repository.upsert_media_file(
                library_entry_id=entry_id,
                relative_path="../escape.mp4",
                size_bytes=1,
                mtime_ns=1,
            ),
            lambda: self.repository.upsert_media_file(
                library_entry_id=entry_id,
                relative_path="Cowboy Bebop/01.mp4",
                size_bytes=-1,
                mtime_ns=1,
            ),
            lambda: self.repository.upsert_library_entry(
                relative_path="Invalid Pin",
                title="Invalid Pin",
                pinned_provider="jikan",
            ),
        )
        for operation in cases:
            with (
                self.subTest(operation=operation),
                self.assertRaises((ValueError, sqlite3.IntegrityError)),
            ):
                operation()

    def test_provider_metadata_queries_and_staleness(self) -> None:
        entry_id = self._entry()
        record_id = self._provider_record(entry_id)
        record = self.repository.get_provider_record(entry_id, "jikan")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(json.loads(record.raw_json)["mal_id"], 1)
        self.assertEqual(record.episode_count, 26)
        self.assertEqual(
            self.repository.list_stale_provider_records(
                "jikan", NOW + timedelta(days=1)
            ),
            [record],
        )
        self.assertEqual(
            self.repository.list_stale_provider_records(
                "jikan", NOW - timedelta(days=1)
            ),
            [],
        )
        self.assertEqual(record_id, record.id)

    def test_alias_genre_relation_and_artwork_replacement(self) -> None:
        record_id = self._provider_record(self._entry())
        self.repository.replace_aliases(
            record_id, [("english", "Cowboy Bebop"), ("japanese", "カウボーイビバップ")]
        )
        self.repository.replace_genres(record_id, ["Sci-Fi", "Action", "action"])
        relations = [
            Relation("sequel", "jikan", "5", "Cowboy Bebop: The Movie"),
            Relation("side_story", "jikan", "17205", "Ein's Summer Vacation"),
        ]
        self.repository.replace_relations(record_id, relations)
        artwork = self.repository.upsert_artwork(
            provider_record_id=record_id,
            kind="cover",
            source_url="https://example.invalid/cover.jpg",
            relative_path="artwork/jikan-1.jpg",
            mime_type="image/jpeg",
            size_bytes=1024,
            etag='"cover"',
            fetched_at=NOW,
        )

        self.assertEqual(
            self.repository.list_aliases(record_id),
            [("english", "Cowboy Bebop"), ("japanese", "カウボーイビバップ")],
        )
        self.assertEqual(self.repository.list_genres(record_id), ["Action", "Sci-Fi"])
        self.assertEqual(self.repository.list_relations(record_id), relations)
        self.assertEqual(artwork.relative_path, "artwork/jikan-1.jpg")

        self.repository.replace_aliases(record_id, [("short", "Bebop")])
        self.repository.replace_genres(record_id, ["Drama"])
        self.repository.replace_relations(record_id, [])
        self.assertEqual(self.repository.list_aliases(record_id), [("short", "Bebop")])
        self.assertEqual(self.repository.list_genres(record_id), ["Drama"])
        self.assertEqual(self.repository.list_relations(record_id), [])

    def test_failed_metadata_replacement_restores_previous_rows(self) -> None:
        record_id = self._provider_record(self._entry())
        self.repository.replace_aliases(record_id, [("english", "Cowboy Bebop")])

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.replace_aliases(
                record_id,
                [("english", "Duplicate"), ("english", "Duplicate")],
            )

        self.assertEqual(
            self.repository.list_aliases(record_id),
            [("english", "Cowboy Bebop")],
        )

    def test_scan_run_lifecycle(self) -> None:
        scan = self.repository.start_scan(started_at=NOW)
        self.assertEqual(scan.status, "running")
        self.assertIsNone(scan.finished_at)

        finished = self.repository.finish_scan(
            scan.id,
            status="partial",
            discovered_entries=2,
            discovered_files=26,
            error_count=1,
            summary="one unreadable directory",
            finished_at=NOW + timedelta(seconds=5),
        )
        self.assertEqual(finished.status, "partial")
        self.assertEqual(finished.discovered_files, 26)
        with self.assertRaises(DatabaseError):
            self.repository.finish_scan(
                scan.id,
                status="success",
                discovered_entries=2,
                discovered_files=26,
            )

    def test_delete_entry_cascades_catalogue_data(self) -> None:
        entry_id = self._entry()
        record_id = self._provider_record(entry_id)
        self.repository.replace_aliases(record_id, [("english", "Bebop")])
        self.repository.replace_genres(record_id, ["Action"])

        # The public API intentionally marks entries unavailable. This direct
        # deletion only verifies the schema's ownership/cascade constraints.
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM library_entries WHERE id = ?", (entry_id,))
        connection.commit()
        connection.close()

        self.assertIsNone(self.repository.get_provider_record(entry_id, "jikan"))


class PathTestCase(unittest.TestCase):
    def test_canonical_relative_paths(self) -> None:
        self.assertEqual(canonical_relative_path("Show/01.mp4"), "Show/01.mp4")
        for value in (
            "",
            "/absolute",
            "../escape",
            "Show/../escape",
            "Show\\episode.mp4",
            "Show//episode.mp4",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_relative_path(value)
