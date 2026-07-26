from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rpi_streamer.database import (
    _MIGRATIONS,
    LATEST_SCHEMA_VERSION,
    CatalogueRepository,
    DatabaseError,
)
from rpi_streamer.generator import generate_site

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _schema_five_database(path: Path, *, invalid_alias: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            applied_at TEXT NOT NULL
        )
        """
    )
    for version in range(1, 6):
        for statement in _MIGRATIONS[version]:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            (version, "2026-01-01T00:00:00+00:00"),
        )
    connection.execute(
        """
        INSERT INTO library_entries(
            id, relative_path, title, sort_title, available,
            metadata_enabled, pinned_provider, pinned_provider_id,
            first_seen_at, last_seen_at
        ) VALUES (
            7, 'MF Ghost', 'MF Ghost', 'MF Ghost', 1, 1, 'jikan', '50695',
            '2026-01-01T00:00:00+00:00', '2026-07-26T12:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_files(
            id, library_entry_id, relative_path, filename, size_bytes,
            mtime_ns, episode_hint, available, first_seen_at, last_seen_at,
            local_identity, inferred_episode_hint, inference_confidence
        ) VALUES (
            11, 7, 'MF Ghost/01.mp4', '01.mp4', 100, 200, '1', 1,
            '2026-01-01T00:00:00+00:00', '2026-07-26T12:00:00+00:00',
            '1:11', NULL, NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO provider_records(
            id, library_entry_id, provider, provider_id, canonical_title,
            synopsis, episode_count, raw_json, etag, last_modified,
            fetched_at, validator_source
        ) VALUES (
            23, 7, 'jikan', '50695', 'MF Ghost', 'Racing.', 12,
            '{"mal_id":50695}', '"etag"', 'Sat, 26 Jul 2026 12:00:00 GMT',
            '2026-07-26T12:00:00+00:00', 'tenrai'
        )
        """
    )
    connection.execute(
        "INSERT INTO aliases VALUES (31, ?, 'english', 'MF Ghost')",
        (999 if invalid_alias else 23,),
    )
    connection.execute("INSERT INTO genres VALUES (41, 'Drama')")
    connection.execute("INSERT INTO provider_record_genres VALUES (23, 41)")
    connection.execute(
        """
        INSERT INTO relations VALUES (
            51, 23, 'sequel', 'jikan', '57559', 'MF Ghost 2nd Season'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO artwork VALUES (
            61, 23, 'cover', 'https://example.invalid/cover.jpg',
            'artwork/cover.jpg', 'image/jpeg', 1234, '"art"', NULL,
            '2026-07-26T12:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO provider_episodes VALUES (
            23, 1, 'The Challenger from England',
            '2023-10-02T00:00:00+00:00', 0, 0
        )
        """
    )
    connection.execute(
        """
        INSERT INTO inference_cache VALUES (
            'digest', 'gpt-5.6-luna', '1', '{}',
            '2026-07-26T12:00:00+00:00'
        )
        """
    )
    connection.commit()
    connection.close()


class MultiWorkMigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "catalogue.db"

    def test_schema_five_migration_preserves_provider_graph_and_ids(self) -> None:
        _schema_five_database(self.path)
        artwork_path = self.path.parent / "artwork" / "cover.jpg"
        artwork_path.parent.mkdir()
        artwork_path.write_bytes(b"x" * 1234)

        with CatalogueRepository(self.path) as repository:
            self.assertEqual(repository.schema_version, LATEST_SCHEMA_VERSION)
            record = repository.get_provider_record(7, "jikan")
            assert record is not None
            self.assertEqual(record.id, 23)
            self.assertEqual(record.validator_source, "tenrai")
            self.assertEqual(repository.list_aliases(23), [("english", "MF Ghost")])
            self.assertEqual(repository.list_genres(23), ["Drama"])
            self.assertEqual(
                repository.list_relations(23)[0].target_provider_id, "57559"
            )
            self.assertEqual(repository.list_provider_episodes(23)[0].episode_number, 1)
            self.assertEqual(repository.get_artwork(23, "cover").id, 61)  # type: ignore[union-attr]
            work = repository.get_primary_library_entry_work(7)
            assert work is not None
            self.assertEqual(work.provider_record_id, 23)
            self.assertEqual(work.source, "manual")
            self.assertIsNone(repository.get_media_work_mapping(11))
            self.assertIsNotNone(repository.get_inference_cache("digest"))
            site = self.path.parent / "site"
            generate_site(repository, site_dir=site, state_dir=self.path.parent)
            title_pages = list((site / "titles").glob("*.html"))
            self.assertEqual(len(title_pages), 1)
            html = title_pages[0].read_text(encoding="utf-8")
            self.assertIn("<title>MF Ghost · RPi Streamer</title>", html)
            self.assertIn("assets/covers/jikan-50695", html)
            self.assertIn("The Challenger from England", html)
            self.assertIn("MF Ghost 2nd Season", html)
            self.assertIn("/media/MF%20Ghost/01.mp4", html)

        with CatalogueRepository(self.path) as reopened:
            self.assertEqual(reopened.schema_version, LATEST_SCHEMA_VERSION)
            self.assertEqual(reopened.get_provider_record(7, "jikan").id, 23)  # type: ignore[union-attr]

    def test_migration_rolls_back_when_foreign_key_check_fails(self) -> None:
        _schema_five_database(self.path, invalid_alias=True)

        with self.assertRaises((DatabaseError, sqlite3.IntegrityError)):
            CatalogueRepository(self.path)

        connection = sqlite3.connect(self.path)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(provider_records)")
        }
        connection.close()
        self.assertEqual(version, 5)
        self.assertIn("library_entry_id", columns)

    def test_normalized_record_can_be_shared_without_duplicate_metadata(self) -> None:
        with CatalogueRepository(self.path) as repository:
            first = repository.upsert_library_entry(
                relative_path="MF Ghost",
                title="MF Ghost",
                seen_at=NOW,
            )
            second = repository.upsert_library_entry(
                relative_path="MF Ghost Backup",
                title="MF Ghost",
                seen_at=NOW,
            )
            record = repository.upsert_provider_record(
                library_entry_id=first.id,
                provider="jikan",
                provider_id="50695",
                canonical_title="MF Ghost",
                raw_data={"mal_id": 50695},
                fetched_at=NOW,
            )
            shared = repository.associate_library_entry_work(
                library_entry_id=second.id,
                provider_record_id=record.id,
                local_name="season-1",
                source="manual",
                verified_at=NOW,
                is_primary=True,
            )

            self.assertEqual(shared.provider_record_id, record.id)
            second_record = repository.get_provider_record(second.id, "jikan")
            assert second_record is not None
            self.assertEqual(second_record.id, record.id)
            connection = sqlite3.connect(self.path)
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_records"
            ).fetchone()[0]
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM library_entries WHERE id = ?", (first.id,))
            connection.commit()
            remaining = connection.execute(
                "SELECT COUNT(*) FROM provider_records WHERE id = ?", (record.id,)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(count, 1)
            self.assertEqual(remaining, 1)

    def test_mapping_constraints_and_cascades(self) -> None:
        with CatalogueRepository(self.path) as repository:
            entry = repository.upsert_library_entry(
                relative_path="MF Ghost", title="MF Ghost", seen_at=NOW
            )
            other = repository.upsert_library_entry(
                relative_path="Other", title="Other", seen_at=NOW
            )
            media = repository.upsert_media_file(
                library_entry_id=entry.id,
                relative_path="MF Ghost/01.mp4",
                size_bytes=1,
                mtime_ns=1,
                seen_at=NOW,
            )
            record = repository.upsert_provider_record(
                library_entry_id=entry.id,
                provider="jikan",
                provider_id="50695",
                canonical_title="MF Ghost",
                raw_data={},
                fetched_at=NOW,
            )
            work = repository.get_primary_library_entry_work(entry.id)
            assert work is not None
            other_record = repository.upsert_provider_record(
                library_entry_id=other.id,
                provider="jikan",
                provider_id="1",
                canonical_title="Other",
                raw_data={},
                fetched_at=NOW,
            )
            other_work = repository.get_primary_library_entry_work(other.id)
            assert (
                other_work is not None
                and other_record.id == other_work.provider_record_id
            )

            with self.assertRaisesRegex(ValueError, "different entries"):
                repository.set_media_work_mapping(
                    media_file_id=media.id,
                    library_entry_work_id=other_work.id,
                    kind="episode",
                    source="deterministic",
                    input_digest="digest",
                    mapped_at=NOW,
                    episode_start=1,
                    episode_end=1,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                repository.set_media_work_mapping(
                    media_file_id=media.id,
                    library_entry_work_id=work.id,
                    kind="episode",
                    source="model",
                    input_digest="digest",
                    mapped_at=NOW,
                    episode_start=1,
                    episode_end=1,
                )
            mapping = repository.set_media_work_mapping(
                media_file_id=media.id,
                library_entry_work_id=work.id,
                kind="episode",
                source="model",
                input_digest="digest",
                mapped_at=NOW,
                episode_start=1,
                episode_end=1,
                confidence=0.9,
                model="gpt-5.6-luna",
                schema_version="1",
            )
            self.assertEqual(mapping.confidence, 0.9)

            connection = sqlite3.connect(self.path)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM media_files WHERE id = ?", (media.id,))
            connection.commit()
            count = connection.execute(
                "SELECT COUNT(*) FROM media_work_mappings"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(count, 0)
            self.assertIsNotNone(
                repository.get_provider_record_by_provider_id(
                    "jikan", record.provider_id
                )
            )

    def test_schema_six_mapping_kinds_migrate_to_public_vocabulary(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                applied_at TEXT NOT NULL
            )
            """
        )
        for version in range(1, 7):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, "2026-01-01T00:00:00+00:00"),
            )
        connection.execute(
            """
            INSERT INTO library_entries VALUES (
                1, 'Show', 'Show', 'Show', 1, 1, NULL, NULL,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO media_files VALUES (
                1, 1, 'Show/recap.mp4', 'recap.mp4', 1, 1, NULL, 1,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, NULL, NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO provider_records VALUES (
                1, 'jikan', '1', 'Show', NULL, 1, '{}', NULL, NULL,
                '2026-01-01T00:00:00+00:00', 'tenrai'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO library_entry_works VALUES (
                1, 1, 1, 1, 'primary', NULL, 0, 'matched', NULL,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO media_work_mappings VALUES (
                1, 1, 'recap', NULL, NULL, NULL, 'deterministic', NULL,
                NULL, NULL, 'digest', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        connection.commit()
        connection.close()

        with CatalogueRepository(self.path) as repository:
            mapping = repository.get_media_work_mapping(1)
            assert mapping is not None
            self.assertEqual(mapping.kind, "summary")
            self.assertEqual(repository.schema_version, LATEST_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
