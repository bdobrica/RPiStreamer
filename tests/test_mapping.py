from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rpi_streamer.database import CatalogueRepository
from rpi_streamer.mapping import (
    MAPPING_SCHEMA_VERSION,
    map_entry_deterministically,
    parse_local_media_facts,
)

NOW = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"


class FilenameFactsTestCase(unittest.TestCase):
    def test_parses_seasons_ranges_and_specials_without_release_noise(self) -> None:
        cases = {
            "Show.S02E03-E04.1080p.x265.2026.mp4": (2, 3, 4, None),
            "Show_2nd_Season_-_01_BD_1080p.mp4": (2, 1, 1, None),
            "Show Season 3 - Episode 07.mp4.mp4": (3, 7, 7, None),
            "[Group] Show OVA 02 [1080p].mp4": (None, 2, 2, "ova"),
            "Show Movie 2024 2160p x264.mp4": (None, None, None, "movie"),
            "Show Battle Digest.mp4": (None, None, None, "summary"),
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                facts = parse_local_media_facts(filename)
                self.assertEqual(
                    (
                        facts.season,
                        facts.episode_start,
                        facts.episode_end,
                        facts.special_kind,
                    ),
                    expected,
                )


class DeterministicMappingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = CatalogueRepository(Path(temporary.name) / "catalogue.db")
        self.addCleanup(self.repository.close)

    def _fixture(self, name: str) -> tuple[int, dict[str, Any]]:
        fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        entry = self.repository.upsert_library_entry(
            relative_path=name, title=name, seen_at=NOW
        )
        for index, definition in enumerate(fixture["works"]):
            if index == 0:
                record = self.repository.upsert_provider_record(
                    library_entry_id=entry.id,
                    provider="jikan",
                    provider_id=str(definition["mal_id"]),
                    canonical_title=definition["title"],
                    episode_count=definition["episode_count"],
                    raw_data={"mal_id": definition["mal_id"]},
                    fetched_at=NOW,
                )
            else:
                record = self.repository.upsert_detached_provider_record(
                    provider="jikan",
                    provider_id=str(definition["mal_id"]),
                    canonical_title=definition["title"],
                    episode_count=definition["episode_count"],
                    raw_data={"mal_id": definition["mal_id"]},
                    fetched_at=NOW,
                    validator_source="tenrai",
                )
                self.repository.associate_library_entry_work(
                    library_entry_id=entry.id,
                    provider_record_id=record.id,
                    local_name=f"relation-{definition['mal_id']}",
                    source="relation",
                    relation_distance=index,
                    display_order=1000 + index,
                    verified_at=NOW,
                )
        filenames = fixture.get("files", [])
        for series in fixture.get("file_series", []):
            filenames.extend(
                series["template"].format(episode=episode)
                for episode in range(series["start"], series["end"] + 1)
            )
        for identity, filename in enumerate(filenames, start=1):
            self.repository.upsert_media_file(
                library_entry_id=entry.id,
                relative_path=f"{name}/{filename}",
                size_bytes=1,
                mtime_ns=identity,
                local_identity=f"1:{identity}",
                seen_at=NOW,
            )
        return entry.id, fixture

    def test_mf_ghost_maps_three_cumulative_ranges(self) -> None:
        entry_id, fixture = self._fixture("mf_ghost_continuous")

        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        mappings = [
            self.repository.get_media_work_mapping(media.id)
            for media in self.repository.list_media_files(entry_id)
        ]
        works = self.repository.list_library_entry_works(entry_id)

        self.assertEqual(result.mapped, fixture["expected"]["file_count"])
        self.assertEqual(
            [mapping.episode_start for mapping in mappings if mapping][11:14],
            [12, 1, 2],
        )
        self.assertEqual(mappings[0].library_entry_work_id, works[0].id)  # type: ignore[union-attr]
        self.assertEqual(mappings[12].library_entry_work_id, works[1].id)  # type: ignore[union-attr]
        self.assertEqual(mappings[24].library_entry_work_id, works[2].id)  # type: ignore[union-attr]
        self.assertTrue(
            all(
                mapping is not None
                and mapping.source == "deterministic"
                and mapping.schema_version == MAPPING_SCHEMA_VERSION
                for mapping in mappings
            )
        )

    def test_tsukimichi_maps_reset_numbering_by_explicit_season(self) -> None:
        entry_id, _ = self._fixture("tsukimichi_reset")

        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        media = self.repository.list_media_files(entry_id)
        mappings = [self.repository.get_media_work_mapping(item.id) for item in media]
        works = self.repository.list_library_entry_works(entry_id)

        self.assertEqual(result.mapped, 37)
        self.assertTrue(
            all(
                mapping is not None and mapping.library_entry_work_id == works[0].id
                for mapping in mappings[:12]
            )
        )
        self.assertTrue(
            all(
                mapping is not None and mapping.library_entry_work_id == works[1].id
                for mapping in mappings[12:]
            )
        )
        self.assertEqual(mappings[12].episode_start, 1)  # type: ignore[union-attr]

    def test_tie_in_kinds_map_only_to_unique_named_candidates(self) -> None:
        entry_id, fixture = self._fixture("tie_ins")

        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        decisions = {item.filename: item for item in result.decisions}
        expected = {item["file"]: item for item in fixture["expected_mappings"]}

        for filename, mapping in expected.items():
            with self.subTest(filename=filename):
                decision = decisions[filename]
                if mapping["mal_id"] is None:
                    self.assertEqual(decision.outcome, "unmapped")
                    continue
                work_record = self.repository.get_provider_record_for_work(
                    decision.work_id  # type: ignore[arg-type]
                )
                assert work_record is not None
                self.assertEqual(decision.outcome, "mapped")
                self.assertEqual(work_record.provider_id, str(mapping["mal_id"]))
                self.assertEqual(decision.kind, mapping["kind"])

    def test_incomplete_cumulative_sequence_is_unmapped(self) -> None:
        entry_id, _ = self._fixture("mf_ghost_continuous")
        last = self.repository.list_media_files(entry_id)[-1]
        self.repository._connection.execute(  # noqa: SLF001
            "DELETE FROM media_files WHERE id = ?", (last.id,)
        )

        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )

        self.assertTrue(all(item.outcome == "unmapped" for item in result.decisions))

    def test_duplicate_numbering_is_ambiguous_without_season_markers(self) -> None:
        entry_id, _ = self._fixture("mf_ghost_continuous")
        media = self.repository.list_media_files(entry_id)[-1]
        self.repository._connection.execute(  # noqa: SLF001
            "UPDATE media_files SET filename = 'duplicate_01.mp4' WHERE id = ?",
            (media.id,),
        )

        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )

        self.assertTrue(all(item.outcome == "ambiguous" for item in result.decisions))

    def test_manual_mapping_is_never_overwritten(self) -> None:
        entry_id, _ = self._fixture("mf_ghost_continuous")
        media = self.repository.list_media_files(entry_id)[0]
        work = self.repository.list_library_entry_works(entry_id)[0]
        self.repository.set_media_work_mapping(
            media_file_id=media.id,
            library_entry_work_id=work.id,
            kind="episode",
            episode_start=9,
            episode_end=9,
            source="manual_exact",
            input_digest="manual",
            mapped_at=NOW,
        )

        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        mapping = self.repository.get_media_work_mapping(media.id)

        assert mapping is not None
        self.assertEqual((mapping.source, mapping.episode_start), ("manual_exact", 9))

    def test_filename_or_provider_count_change_invalidates_digest(self) -> None:
        entry_id, _ = self._fixture("mf_ghost_continuous")
        media = self.repository.list_media_files(entry_id)[0]
        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        first = self.repository.get_media_work_mapping(media.id)
        assert first is not None
        self.repository._connection.execute(  # noqa: SLF001
            "UPDATE media_files SET filename = 'renamed_01.mp4' WHERE id = ?",
            (media.id,),
        )

        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        renamed = self.repository.get_media_work_mapping(media.id)
        assert renamed is not None
        self.assertNotEqual(renamed.input_digest, first.input_digest)

        final_work = self.repository.list_library_entry_works(entry_id)[-1]
        record = self.repository.get_provider_record_for_work(final_work.id)
        assert record is not None
        self.repository._connection.execute(  # noqa: SLF001
            "UPDATE provider_records SET episode_count = 12 WHERE id = ?",
            (record.id,),
        )
        result = map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        self.assertTrue(all(item.outcome == "unmapped" for item in result.decisions))
        self.assertIsNone(self.repository.get_media_work_mapping(media.id))


if __name__ == "__main__":
    unittest.main()
