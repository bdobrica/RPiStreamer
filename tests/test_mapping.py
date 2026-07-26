from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rpi_streamer.database import CatalogueRepository
from rpi_streamer.inference import (
    MAPPING_SCHEMA_VERSION as MODEL_MAPPING_SCHEMA_VERSION,
)
from rpi_streamer.inference import (
    InferenceError,
    OpenAIInferenceClient,
)
from rpi_streamer.mapping import (
    MAPPING_SCHEMA_VERSION,
    map_entry_deterministically,
    map_entry_with_model,
    parse_local_media_facts,
)

NOW = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"


def _model_response(mappings: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "schema_version": MODEL_MAPPING_SCHEMA_VERSION,
                                    "mappings": mappings,
                                }
                            ),
                        }
                    ],
                }
            ],
        }
    ).encode()


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

    def test_model_maps_ambiguous_tie_in_from_verified_candidates_and_caches(
        self,
    ) -> None:
        entry_id, _ = self._fixture("tie_ins")
        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        calls = 0

        def transport(request: urllib.request.Request, _timeout: float) -> bytes:
            nonlocal calls
            calls += 1
            body = json.loads(request.data.decode())  # type: ignore[union-attr]
            submitted = json.loads(body["input"])["files"]
            mappings = []
            for facts in submitted:
                is_bonus = facts["filename"] == "Fixture_Show_Bonus.mp4"
                accepted = is_bonus and calls == 1
                mappings.append(
                    {
                        "filename": facts["filename"],
                        "mal_id": "992003" if accepted else None,
                        "kind": "ova" if accepted else "unknown",
                        "episode_start": 1 if accepted else None,
                        "episode_end": 1 if accepted else None,
                        "confidence": 0.96 if accepted else 0.4,
                        "reason": "bonus OVA" if accepted else "uncertain",
                    }
                )
            return _model_response(mappings)

        inference = OpenAIInferenceClient("key", transport=transport)
        first = map_entry_with_model(
            self.repository,
            entry_id,
            inference,
            mapped_at=NOW,
            rules_digest="rules",
            cache_ttl=86400,
        )
        bonus = next(
            media
            for media in self.repository.list_media_files(entry_id)
            if media.filename == "Fixture_Show_Bonus.mp4"
        )
        mapping = self.repository.get_media_work_mapping(bonus.id)
        assert mapping is not None
        record = self.repository.get_provider_record_for_work(
            mapping.library_entry_work_id
        )
        assert record is not None

        self.assertEqual(first.applied, 1)
        self.assertEqual((mapping.source, record.provider_id), ("model", "992003"))
        self.assertEqual(mapping.confidence, 0.96)
        self.assertEqual(calls, 1)

        cached_client = OpenAIInferenceClient(
            "key",
            transport=lambda _request, _timeout: self.fail("unexpected model call"),
        )
        second = map_entry_with_model(
            self.repository,
            entry_id,
            cached_client,
            mapped_at=NOW,
            rules_digest="rules",
            cache_ttl=86400,
        )
        self.assertTrue(second.cache_hit)
        self.assertEqual(cached_client.calls, 0)

        invalidated = map_entry_with_model(
            self.repository,
            entry_id,
            inference,
            mapped_at=NOW,
            rules_digest="changed-rules",
            cache_ttl=86400,
        )
        self.assertFalse(invalidated.cache_hit)
        self.assertEqual(invalidated.applied, 0)
        self.assertEqual(calls, 2)
        self.assertIsNone(self.repository.get_media_work_mapping(bonus.id))

    def test_model_rejects_provider_overflow_and_low_confidence(self) -> None:
        entry_id, _ = self._fixture("tie_ins")
        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )

        def transport(request: urllib.request.Request, _timeout: float) -> bytes:
            body = json.loads(request.data.decode())  # type: ignore[union-attr]
            submitted = json.loads(body["input"])["files"]
            return _model_response(
                [
                    {
                        "filename": facts["filename"],
                        "mal_id": "992003",
                        "kind": "episode",
                        "episode_start": 2,
                        "episode_end": 2,
                        "confidence": (
                            0.95
                            if facts["filename"] == "Fixture_Show_Bonus.mp4"
                            else 0.5
                        ),
                        "reason": "candidate",
                    }
                    for facts in submitted
                ]
            )

        result = map_entry_with_model(
            self.repository,
            entry_id,
            OpenAIInferenceClient("key", transport=transport),
            mapped_at=NOW,
            rules_digest="rules",
            cache_ttl=86400,
        )

        self.assertEqual(result.applied, 0)
        self.assertIn("exceeds verified provider count", result.errors[0])

    def test_deterministic_fixture_spends_no_model_call(self) -> None:
        entry_id, _ = self._fixture("mf_ghost_continuous")
        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        inference = OpenAIInferenceClient(
            "key",
            transport=lambda _request, _timeout: self.fail("unexpected model call"),
        )

        result = map_entry_with_model(
            self.repository,
            entry_id,
            inference,
            mapped_at=NOW,
            rules_digest="rules",
            cache_ttl=86400,
        )

        self.assertEqual(result.applied, 0)
        self.assertEqual(inference.calls, 0)

    def test_transport_failure_enters_short_cooldown(self) -> None:
        entry_id, _ = self._fixture("tie_ins")
        map_entry_deterministically(
            self.repository, entry_id, mapped_at=NOW, rules_digest="rules"
        )
        calls = 0

        def transport(_request: urllib.request.Request, _timeout: float) -> bytes:
            nonlocal calls
            calls += 1
            raise TimeoutError("timed out")

        with self.assertRaises(InferenceError):
            map_entry_with_model(
                self.repository,
                entry_id,
                OpenAIInferenceClient("key", transport=transport),
                mapped_at=NOW,
                rules_digest="rules",
                cache_ttl=86400,
            )
        second = map_entry_with_model(
            self.repository,
            entry_id,
            OpenAIInferenceClient("key", transport=transport),
            mapped_at=NOW,
            rules_digest="rules",
            cache_ttl=86400,
        )

        self.assertEqual(calls, 1)
        self.assertIn("cooldown", second.errors[0])


if __name__ == "__main__":
    unittest.main()
