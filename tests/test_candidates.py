from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.candidates import (
    MAX_CANDIDATE_WORKS,
    discover_related_candidates,
    multi_work_suspicion,
)
from rpi_streamer.database import (
    CatalogueRepository,
    LibraryEntry,
    ProviderRecord,
    Relation,
)
from rpi_streamer.scanner import scan_library

NOW = datetime(2026, 7, 26, 16, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"


class CandidateDiscoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = CatalogueRepository(
            Path(self.temporary.name) / "catalogue.db"
        )
        self.addCleanup(self.repository.close)

    def _entry(
        self, *, provider_id: str = "1", episode_count: int | None = 12
    ) -> tuple[LibraryEntry, ProviderRecord]:
        entry = self.repository.upsert_library_entry(
            relative_path="Collection", title="Collection", seen_at=NOW
        )
        record = self.repository.upsert_provider_record(
            library_entry_id=entry.id,
            provider="jikan",
            provider_id=provider_id,
            canonical_title=f"Work {provider_id}",
            episode_count=episode_count,
            raw_data={"mal_id": int(provider_id)},
            fetched_at=NOW,
        )
        return entry, record

    def _detached(
        self,
        provider_id: str,
        *,
        episode_count: int | None = 12,
        relations: tuple[Relation, ...] = (),
    ) -> ProviderRecord:
        record = self.repository.upsert_detached_provider_record(
            provider="jikan",
            provider_id=provider_id,
            canonical_title=f"Work {provider_id}",
            episode_count=episode_count,
            raw_data={"mal_id": int(provider_id)},
            fetched_at=NOW,
            validator_source="tenrai",
        )
        self.repository.replace_relations(record.id, relations)
        return record

    def test_ordinary_single_work_skips_expansion(self) -> None:
        entry, primary = self._entry()
        stale = self._detached("2")
        self.repository.associate_library_entry_work(
            library_entry_id=entry.id,
            provider_record_id=stale.id,
            local_name="relation-2",
            source="relation",
            relation_distance=1,
            verified_at=NOW,
        )
        calls: list[str] = []

        def verifier(
            _repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[None, None]:
            calls.append(provider_id)
            return None, None

        result = discover_related_candidates(
            self.repository,
            entry,
            ["01.mp4", "02.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=verifier,
        )

        self.assertFalse(result.expanded)
        self.assertEqual(result.associated, 0)
        self.assertEqual(calls, [])
        self.assertIsNone(
            self.repository.get_library_entry_work_by_provider_id(
                entry.id, "jikan", "2"
            )
        )
        self.assertFalse(
            multi_work_suspicion(
                ["01.mp4"], primary, (), has_manual_candidates=False
            ).suspected
        )

    def test_sequel_chain_is_cycle_safe_and_stores_distance(self) -> None:
        entry, primary = self._entry()
        self.repository.replace_relations(
            primary.id, [Relation("sequel", "jikan", "2", "Work 2")]
        )
        graph = {
            "2": (Relation("Sequel", "jikan", "3", "Work 3"),),
            "3": (Relation("Prequel", "jikan", "1", "Work 1"),),
        }
        calls: list[str] = []

        def verifier(
            _repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[ProviderRecord, None]:
            calls.append(provider_id)
            return self._detached(provider_id, relations=graph[provider_id]), None

        result = discover_related_candidates(
            self.repository,
            entry,
            [f"{number:02d}.mp4" for number in range(1, 14)],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=verifier,
        )
        works = self.repository.list_library_entry_works(entry.id)
        by_id = {
            self.repository.get_provider_record_for_work(work.id).provider_id: work  # type: ignore[union-attr]
            for work in works
        }

        self.assertTrue(result.expanded)
        self.assertEqual(result.associated, 2)
        self.assertEqual(calls, ["2", "3"])
        self.assertEqual(by_id["2"].relation_distance, 1)
        self.assertEqual(by_id["3"].relation_distance, 2)
        self.assertEqual(by_id["2"].source, "relation")

    def test_relation_filter_duplicates_priority_depth_and_count_bounds(self) -> None:
        entry, primary = self._entry()
        self.repository.replace_relations(
            primary.id,
            [
                Relation("other", "jikan", "2", "Duplicate"),
                Relation("sequel", "jikan", "2", "Preferred"),
                Relation("adaptation", "jikan", "90", "Not allowed"),
                Relation("sequel", "manga", "91", "Not anime"),
                Relation("sequel", "jikan", "3", "Second"),
            ],
        )
        calls: list[str] = []

        def verifier(
            _repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[ProviderRecord, None]:
            calls.append(provider_id)
            next_id = str(int(provider_id) + 1)
            relations = (
                (Relation("sequel", "jikan", next_id, f"Work {next_id}"),)
                if int(provider_id) < 8
                else ()
            )
            return self._detached(provider_id, relations=relations), None

        result = discover_related_candidates(
            self.repository,
            entry,
            ["Season 2 - 01.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=verifier,
            max_depth=2,
            max_candidates=3,
        )

        self.assertTrue(result.expanded)
        self.assertEqual(calls, ["2", "3"])
        self.assertNotIn("90", calls)
        self.assertNotIn("91", calls)
        self.assertLessEqual(len(self.repository.list_library_entry_works(entry.id)), 3)
        self.assertLessEqual(
            len(self.repository.list_library_entry_works(entry.id)),
            MAX_CANDIDATE_WORKS,
        )

    def test_cached_target_works_offline_without_verifier(self) -> None:
        entry, primary = self._entry()
        cached = self._detached("2")
        self.repository.replace_relations(
            primary.id, [Relation("side story", "jikan", "2", "Cached")]
        )

        result = discover_related_candidates(
            self.repository,
            entry,
            ["Movie.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=None,
        )
        work = self.repository.get_library_entry_work_by_provider_id(
            entry.id, "jikan", "2"
        )

        self.assertEqual(result.errors, ())
        assert work is not None
        self.assertEqual(work.provider_record_id, cached.id)

    def test_relation_depth_stops_before_fourth_hop(self) -> None:
        entry, primary = self._entry()
        self.repository.replace_relations(
            primary.id, [Relation("sequel", "jikan", "2", "Work 2")]
        )
        calls: list[str] = []

        def verifier(
            _repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[ProviderRecord, None]:
            calls.append(provider_id)
            next_id = str(int(provider_id) + 1)
            return (
                self._detached(
                    provider_id,
                    relations=(
                        Relation("sequel", "jikan", next_id, f"Work {next_id}"),
                    ),
                ),
                None,
            )

        discover_related_candidates(
            self.repository,
            entry,
            ["Season 2 - 01.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=verifier,
        )

        self.assertEqual(calls, ["2", "3", "4"])
        self.assertIsNone(
            self.repository.get_library_entry_work_by_provider_id(
                entry.id, "jikan", "5"
            )
        )

    def test_transient_failure_is_partial_and_retains_previous_candidates(self) -> None:
        entry, primary = self._entry()
        cached = self._detached("2")
        self.repository.replace_relations(
            primary.id, [Relation("sequel", "jikan", "2", "Cached")]
        )
        discover_related_candidates(
            self.repository,
            entry,
            ["13.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=None,
        )
        self.repository.replace_relations(
            primary.id, [Relation("sequel", "jikan", "999", "Unavailable")]
        )

        result = discover_related_candidates(
            self.repository,
            entry,
            ["13.mp4"],
            has_manual_candidates=False,
            verified_at=NOW,
            verify_work=lambda _repository, _provider_id, _now: (
                None,
                "temporary outage",
            ),
        )

        self.assertIn("temporary outage", result.errors[0])
        retained = self.repository.get_library_entry_work_by_provider_id(
            entry.id, "jikan", "2"
        )
        assert retained is not None
        self.assertEqual(retained.provider_record_id, cached.id)

    def test_fixture_layouts_discover_expected_sequel_candidates(self) -> None:
        for fixture_name in ("mf_ghost_continuous", "tsukimichi_reset"):
            with self.subTest(fixture=fixture_name):
                fixture = json.loads(
                    (FIXTURES / f"{fixture_name}.json").read_text(encoding="utf-8")
                )
                path = f"Collection-{fixture_name}"
                entry = self.repository.upsert_library_entry(
                    relative_path=path, title=path, seen_at=NOW
                )
                works = fixture["works"]
                primary = self.repository.upsert_provider_record(
                    library_entry_id=entry.id,
                    provider="jikan",
                    provider_id=str(works[0]["mal_id"]),
                    canonical_title=works[0]["title"],
                    episode_count=works[0]["episode_count"],
                    raw_data={"mal_id": works[0]["mal_id"]},
                    fetched_at=NOW,
                )
                self.repository.replace_relations(
                    primary.id,
                    [
                        Relation(
                            "sequel",
                            "jikan",
                            str(works[1]["mal_id"]),
                            works[1]["title"],
                        )
                    ],
                )
                graph = {
                    str(work["mal_id"]): (
                        (
                            Relation(
                                "sequel",
                                "jikan",
                                str(works[index + 1]["mal_id"]),
                                works[index + 1]["title"],
                            ),
                        )
                        if index + 1 < len(works)
                        else ()
                    )
                    for index, work in enumerate(works[1:], start=1)
                }
                filenames = [
                    series["template"].format(episode=episode)
                    for series in fixture["file_series"]
                    for episode in range(series["start"], series["end"] + 1)
                ]
                calls: list[str] = []

                def verifier(
                    _repository: CatalogueRepository,
                    provider_id: str,
                    _now: datetime,
                ) -> tuple[ProviderRecord, None]:
                    calls.append(provider_id)  # noqa: B023
                    work = next(
                        item  # noqa: B023
                        for item in works  # noqa: B023
                        if str(item["mal_id"]) == provider_id
                    )
                    return (
                        self._detached(
                            provider_id,
                            episode_count=work["episode_count"],
                            relations=graph[provider_id],  # noqa: B023
                        ),
                        None,
                    )

                result = discover_related_candidates(
                    self.repository,
                    entry,
                    filenames,
                    has_manual_candidates=False,
                    verified_at=NOW,
                    verify_work=verifier,
                )

                self.assertEqual(result.errors, ())
                self.assertEqual(len(calls), len(works) - 1)
                self.assertEqual(
                    len(self.repository.list_library_entry_works(entry.id)),
                    len(works),
                )

    def test_scan_integration_discovers_candidates_without_openai(self) -> None:
        media_root = Path(self.temporary.name) / "media"
        folder = media_root / "Collection"
        folder.mkdir(parents=True)
        (folder / "Show_2nd_Season_-_01.mp4").write_bytes(b"x")

        def enrich(repository: CatalogueRepository, scanned_at: datetime) -> tuple[()]:
            entry = repository.get_library_entry("Collection")
            assert entry is not None
            primary = repository.upsert_provider_record(
                library_entry_id=entry.id,
                provider="jikan",
                provider_id="1",
                canonical_title="Primary",
                episode_count=12,
                raw_data={"mal_id": 1},
                fetched_at=scanned_at,
            )
            repository.replace_relations(
                primary.id, [Relation("sequel", "jikan", "2", "Second")]
            )
            return ()

        def verifier(
            _repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[ProviderRecord, None]:
            return self._detached(provider_id), None

        with patch(
            "rpi_streamer.inference.OpenAIInferenceClient.infer"
        ) as model_inference:
            result = scan_library(
                self.repository,
                media_root,
                scanned_at=NOW,
                enrich=enrich,
                verify_work=verifier,
            )

        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        self.assertEqual(result.status, "success")
        self.assertIsNotNone(
            self.repository.get_library_entry_work_by_provider_id(
                entry.id, "jikan", "2"
            )
        )
        model_inference.assert_not_called()


if __name__ == "__main__":
    unittest.main()
