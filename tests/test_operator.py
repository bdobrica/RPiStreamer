from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rpi_streamer.database import CatalogueRepository, Relation
from rpi_streamer.operator import (
    inspect_collection,
    invalidate_model,
    refresh_candidates,
)

NOW = datetime(2026, 7, 26, 17, 0, tzinfo=UTC)


class OperatorControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = CatalogueRepository(
            Path(self.temporary.name) / "catalogue.db"
        )
        self.addCleanup(self.repository.close)

    def _model_mapping(self, collection: str, provider_id: str, digest: str) -> int:
        entry = self.repository.upsert_library_entry(
            relative_path=collection,
            title=collection,
            seen_at=NOW,
        )
        media = self.repository.upsert_media_file(
            library_entry_id=entry.id,
            relative_path=f"{collection}/01.mp4",
            size_bytes=1,
            mtime_ns=1,
            local_identity=f"identity:{collection}",
            seen_at=NOW,
        )
        record = self.repository.upsert_provider_record(
            library_entry_id=entry.id,
            provider="jikan",
            provider_id=provider_id,
            canonical_title=collection,
            raw_data={"mal_id": int(provider_id)},
            fetched_at=NOW,
        )
        work = self.repository.get_primary_library_entry_work(entry.id)
        assert work is not None and work.provider_record_id == record.id
        self.repository.set_media_work_mapping(
            media_file_id=media.id,
            library_entry_work_id=work.id,
            kind="episode",
            episode_start=1,
            episode_end=1,
            source="model",
            confidence=0.95,
            model="gpt-5.6-luna",
            schema_version="test",
            input_digest=digest,
            mapped_at=NOW,
        )
        self.repository.put_inference_cache(
            digest,
            model="gpt-5.6-luna",
            schema_version="test",
            result={"result": collection},
            created_at=NOW,
        )
        return media.id

    def test_model_invalidation_is_limited_to_selected_collection(self) -> None:
        first_media = self._model_mapping("First", "1", "digest-first")
        second_media = self._model_mapping("Second", "2", "digest-second")

        result = invalidate_model(self.repository, "First")

        self.assertEqual(result["model_mappings_removed"], 1)
        self.assertEqual(result["model_cache_entries_removed"], 1)
        self.assertIsNone(self.repository.get_media_work_mapping(first_media))
        self.assertIsNone(self.repository.get_inference_cache("digest-first"))
        self.assertIsNotNone(self.repository.get_media_work_mapping(second_media))
        self.assertIsNotNone(self.repository.get_inference_cache("digest-second"))
        self.assertIsNotNone(
            self.repository.get_provider_record_by_provider_id("jikan", "1")
        )

    def test_inspection_keeps_active_model_mapping_separate_from_preview(
        self,
    ) -> None:
        self._model_mapping("Collection", "1", "digest")
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        related = self.repository.upsert_detached_provider_record(
            provider="jikan",
            provider_id="2",
            canonical_title="Collection Part 2",
            raw_data={"mal_id": 2},
            fetched_at=NOW,
            validator_source="tenrai",
        )
        self.repository.associate_library_entry_work(
            library_entry_id=entry.id,
            provider_record_id=related.id,
            local_name="part-2",
            source="relation",
            relation_distance=1,
            verified_at=NOW,
        )

        result = inspect_collection(
            self.repository, Path(self.temporary.name) / "media", "Collection"
        )
        files = result["files"]
        assert isinstance(files, list)
        inspected = files[0]

        self.assertEqual(inspected["outcome"], "mapped")
        self.assertEqual(inspected["reason"], "active model mapping")
        self.assertEqual(inspected["source"], "model")
        self.assertEqual(inspected["mal_id"], "1")
        self.assertEqual(inspected["deterministic_outcome"], "unmapped")
        self.assertIn("candidate boundaries", inspected["deterministic_reason"])

    def test_candidate_refresh_associates_only_the_selected_collection(self) -> None:
        first_media = self._model_mapping("First", "10", "digest-first")
        self._model_mapping("Second", "20", "digest-second")
        target = self.repository.upsert_detached_provider_record(
            provider="jikan",
            provider_id="11",
            canonical_title="First Season 2",
            episode_count=1,
            raw_data={"mal_id": 11},
            fetched_at=NOW,
        )
        primary = self.repository.get_provider_record_by_provider_id("jikan", "10")
        assert primary is not None
        self.repository.replace_relations(
            primary.id,
            [Relation("sequel", "jikan", "11", "First Season 2")],
        )

        result = refresh_candidates(
            self.repository,
            "First",
            verify_work=None,
            refreshed_at=NOW,
        )

        first = self.repository.get_library_entry("First")
        second = self.repository.get_library_entry("Second")
        assert first is not None and second is not None
        self.assertTrue(result["expanded"])
        self.assertEqual(result["associated"], 1)
        self.assertEqual(
            self.repository.get_library_entry_work_by_provider_id(
                first.id, "jikan", "11"
            ).provider_record_id,  # type: ignore[union-attr]
            target.id,
        )
        self.assertIsNone(
            self.repository.get_library_entry_work_by_provider_id(
                second.id, "jikan", "11"
            )
        )
        self.assertIsNotNone(self.repository.get_media_work_mapping(first_media))

    def test_shared_model_cache_survives_while_another_collection_references_it(
        self,
    ) -> None:
        self._model_mapping("First", "101", "shared-digest")
        second_media = self._model_mapping("Second", "102", "shared-digest")

        result = invalidate_model(self.repository, "First")

        self.assertEqual(result["model_mappings_removed"], 1)
        self.assertEqual(result["model_cache_entries_removed"], 0)
        self.assertIsNotNone(self.repository.get_media_work_mapping(second_media))
        self.assertIsNotNone(self.repository.get_inference_cache("shared-digest"))
