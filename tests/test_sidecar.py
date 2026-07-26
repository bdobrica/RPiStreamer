from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rpi_streamer.database import CatalogueRepository, ProviderRecord
from rpi_streamer.scanner import discover, scan_library
from rpi_streamer.sidecar import read_sidecar, validate_local_files

NOW = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"


class SidecarParserTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "rpi-streamer.ini"

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_legacy_sidecar_remains_compatible(self) -> None:
        self._write(
            "[rpi-streamer]\n"
            "display_title =  Cowboy Bebop  \n"
            "sort_title = Bebop, Cowboy\n"
            "metadata_enabled = yes\n"
            "mal_id = 0001\n"
        )

        sidecar = read_sidecar(self.path)

        self.assertEqual(sidecar.display_title, "Cowboy Bebop")
        self.assertEqual(sidecar.sort_title, "Bebop, Cowboy")
        self.assertTrue(sidecar.metadata_enabled)
        self.assertEqual(sidecar.mal_id, "1")
        self.assertEqual(sidecar.works, ())

    def test_multiple_rules_multiline_globs_ranges_and_exact_unicode(self) -> None:
        self._write(
            "[rpi-streamer]\nmal_id=1\nrelated_mal_ids=2, 3, 2\n"
            '[work "season-2"]\n'
            "mal_id=2\nlabel=Second Season\n"
            "files=\n  *2nd_Season*\n  *S02*\n"
            "season=2\nlocal_episode_range=13-24\n"
            "episode_offset=-12\nkind=episode\norder=20\n"
            '[media "special"]\n'
            "file=特別編 01.mp4\nmal_id=3\nepisode=1\n"
            "kind=special\nlabel=特別編\n"
        )

        sidecar = read_sidecar(self.path)

        self.assertEqual(sidecar.related_mal_ids, ("2", "3"))
        rule = sidecar.works[0]
        self.assertEqual(rule.files, ("*2nd_Season*", "*S02*"))
        self.assertEqual(
            (rule.season, rule.local_episode_start, rule.local_episode_end),
            (2, 13, 24),
        )
        self.assertEqual(rule.episode_offset, -12)
        self.assertEqual(sidecar.media[0].file, "特別編 01.mp4")
        self.assertEqual(sidecar.media[0].episode_end, 1)
        validate_local_files(sidecar, {"Show_2nd_Season_S02_13.mp4", "特別編 01.mp4"})

    def test_unknown_duplicate_invalid_and_excessive_values_are_rejected(self) -> None:
        cases = {
            "unknown key": "[rpi-streamer]\nwat=1\n",
            "unknown section": "[rpi-streamer]\n[other]\na=1\n",
            "duplicate file": (
                "[rpi-streamer]\nmal_id=1\n"
                '[media "a"]\nfile=x.mp4\nmal_id=1\n'
                '[media "b"]\nfile=x.mp4\nmal_id=1\n'
            ),
            "path file": (
                '[rpi-streamer]\nmal_id=1\n[media "a"]\nfile=../x.mp4\nmal_id=1\n'
            ),
            "path glob": (
                '[rpi-streamer]\nmal_id=1\n[work "a"]\nmal_id=1\nfiles=folder/*.mp4\n'
            ),
            "bad range": (
                "[rpi-streamer]\nmal_id=1\n"
                '[work "a"]\nmal_id=1\nlocal_episode_range=24-13\n'
            ),
            "bad offset": (
                "[rpi-streamer]\nmal_id=1\n"
                '[work "a"]\nmal_id=1\nseason=1\nepisode_offset=10000\n'
            ),
            "bad kind": (
                '[rpi-streamer]\nmal_id=1\n[work "a"]\nmal_id=1\nseason=1\nkind=music\n'
            ),
            "too many candidates": (
                "[rpi-streamer]\nmal_id=1\nrelated_mal_ids="
                + ",".join(str(number) for number in range(2, 15))
            ),
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                self._write(value)
                with self.assertRaises(ValueError):
                    read_sidecar(self.path)

    def test_missing_exact_file_and_unmatched_glob_are_rejected(self) -> None:
        self._write(
            "[rpi-streamer]\nmal_id=1\n"
            '[work "s2"]\nmal_id=1\nfiles=*Season_2*\n'
            '[media "missing"]\nfile=missing.mp4\nmal_id=1\n'
        )
        sidecar = read_sidecar(self.path)

        with self.assertRaisesRegex(ValueError, "does not exist"):
            validate_local_files(sidecar, {"01.mp4"})


class ManualMappingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.repository = CatalogueRepository(self.root / "catalogue.db")
        self.addCleanup(self.repository.close)

    def _record(self, mal_id: int, title: str, episode_count: int | None) -> None:
        self.repository.upsert_detached_provider_record(
            provider="jikan",
            provider_id=str(mal_id),
            canonical_title=title,
            episode_count=episode_count,
            raw_data={"mal_id": mal_id, "type": "TV"},
            validator_source="tenrai",
            fetched_at=NOW,
        )

    def _fixture(self, name: str) -> tuple[Path, dict[str, object]]:
        fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        folder = self.media_root / str(fixture["collection"])
        folder.mkdir()
        for series in fixture.get("file_series", []):
            for episode in range(series["start"], series["end"] + 1):
                (folder / series["template"].format(episode=episode)).write_bytes(b"x")
        for item in fixture.get("files", []):
            (folder / item["filename"]).write_bytes(b"x")
        (folder / "rpi-streamer.ini").write_text(
            (FIXTURES / f"{name}.ini").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for work in fixture["works"]:
            self._record(work["mal_id"], work["title"], work["episode_count"])
        return folder, fixture

    def _mappings(self, folder: Path) -> dict[str, tuple[str, int | None, str]]:
        entry = self.repository.get_library_entry(folder.name)
        assert entry is not None
        works = {
            work.id: self.repository.get_provider_record_for_work(work.id)
            for work in self.repository.list_library_entry_works(entry.id)
        }
        result: dict[str, tuple[str, int | None, str]] = {}
        for media in self.repository.list_media_files(entry.id):
            mapping = self.repository.get_media_work_mapping(media.id)
            if mapping is None:
                continue
            record = works[mapping.library_entry_work_id]
            assert record is not None
            result[media.filename] = (
                record.provider_id,
                mapping.episode_start,
                mapping.source,
            )
        return result

    def test_mf_ghost_cumulative_ranges_map_three_works(self) -> None:
        folder, _fixture = self._fixture("mf_ghost_continuous")

        result = scan_library(self.repository, self.media_root, scanned_at=NOW)
        mappings = self._mappings(folder)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(mappings), 37)
        self.assertEqual(
            mappings["Fixture_MF_Ghost_-_01_1080p.mp4"],
            ("990001", 1, "manual_rule"),
        )
        self.assertEqual(
            mappings["Fixture_MF_Ghost_-_13_1080p.mp4"],
            ("990002", 1, "manual_rule"),
        )
        self.assertEqual(
            mappings["Fixture_MF_Ghost_-_37_1080p.mp4"],
            ("990003", 13, "manual_rule"),
        )

    def test_tsukimichi_season_globs_map_reset_numbering(self) -> None:
        folder, _fixture = self._fixture("tsukimichi_reset")

        result = scan_library(self.repository, self.media_root, scanned_at=NOW)
        mappings = self._mappings(folder)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(mappings), 37)
        self.assertEqual(
            mappings["Fixture_Tsukimichi_-_12_1080p.mp4"][:2], ("991001", 12)
        )
        self.assertEqual(
            mappings["Fixture_Tsukimichi_2nd_Season_-_01_1080p.mp4"][:2],
            ("991002", 1),
        )

    def test_exact_override_wins_and_unchanged_mapping_is_stable(self) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "Show_S02E01.mp4").write_bytes(b"x")
        (folder / "rpi-streamer.ini").write_text(
            "[rpi-streamer]\nmal_id=1\nrelated_mal_ids=2\n"
            '[work "season-2"]\nmal_id=1\nseason=2\n'
            '[media "override"]\nfile=Show_S02E01.mp4\n'
            "mal_id=2\nepisode=7\nkind=ova\n",
            encoding="utf-8",
        )
        self._record(1, "Primary", 12)
        self._record(2, "OVA", 1)

        scan_library(self.repository, self.media_root, scanned_at=NOW)
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        media = self.repository.list_media_files(entry.id)[0]
        first = self.repository.get_media_work_mapping(media.id)
        assert first is not None
        scan_library(
            self.repository,
            self.media_root,
            scanned_at=NOW + timedelta(minutes=1),
        )
        second = self.repository.get_media_work_mapping(media.id)

        assert second is not None
        self.assertEqual((first.source, first.episode_start), ("manual_exact", 7))
        self.assertEqual(first.updated_at, second.updated_at)

    def test_overlapping_rules_and_offline_ids_are_partial_and_preserve_mapping(
        self,
    ) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "Show_S01E01.mp4").write_bytes(b"x")
        sidecar = folder / "rpi-streamer.ini"
        sidecar.write_text(
            '[rpi-streamer]\nmal_id=1\n[work "one"]\nmal_id=1\nseason=1\n',
            encoding="utf-8",
        )
        self._record(1, "Primary", 12)
        scan_library(self.repository, self.media_root, scanned_at=NOW)
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        media = self.repository.list_media_files(entry.id)[0]
        previous = self.repository.get_media_work_mapping(media.id)
        assert previous is not None

        sidecar.write_text(
            "[rpi-streamer]\nmal_id=1\nrelated_mal_ids=999\n"
            '[work "one"]\nmal_id=1\nseason=1\n'
            '[work "two"]\nmal_id=1\nfiles=*.mp4\n',
            encoding="utf-8",
        )
        result = scan_library(
            self.repository,
            self.media_root,
            scanned_at=NOW + timedelta(minutes=1),
        )

        retained = self.repository.get_media_work_mapping(media.id)
        self.assertEqual(result.status, "partial")
        self.assertIn("pending", result.summary or "")
        self.assertIn("multiple manual work rules", result.summary or "")
        self.assertEqual(retained, previous)

    def test_uncached_manual_id_uses_verifier_then_maps(self) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "01.mp4").write_bytes(b"x")
        (folder / "rpi-streamer.ini").write_text(
            "[rpi-streamer]\nmal_id=77\n"
            '[work "primary"]\nmal_id=77\nlocal_episode_range=1-12\n',
            encoding="utf-8",
        )
        verified: list[str] = []

        def verifier(
            repository: CatalogueRepository, provider_id: str, _now: datetime
        ) -> tuple[ProviderRecord, None]:
            verified.append(provider_id)
            return (
                repository.upsert_detached_provider_record(
                    provider="jikan",
                    provider_id=provider_id,
                    canonical_title="Verified",
                    episode_count=12,
                    raw_data={"mal_id": int(provider_id)},
                    fetched_at=NOW,
                    validator_source="tenrai",
                ),
                None,
            )

        result = scan_library(
            self.repository,
            self.media_root,
            scanned_at=NOW,
            verify_work=verifier,
        )
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        media = self.repository.list_media_files(entry.id)[0]

        self.assertEqual(result.status, "success")
        self.assertEqual(verified, ["77"])
        self.assertIsNotNone(self.repository.get_media_work_mapping(media.id))

    def test_removed_rule_removes_only_its_manual_mapping(self) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "01.mp4").write_bytes(b"x")
        sidecar = folder / "rpi-streamer.ini"
        sidecar.write_text(
            "[rpi-streamer]\nmal_id=1\n"
            '[work "primary"]\nmal_id=1\nlocal_episode_range=1-12\n',
            encoding="utf-8",
        )
        self._record(1, "Primary", 12)
        scan_library(self.repository, self.media_root, scanned_at=NOW)
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        media = self.repository.list_media_files(entry.id)[0]
        self.assertIsNotNone(self.repository.get_media_work_mapping(media.id))

        sidecar.write_text("[rpi-streamer]\nmal_id=1\n", encoding="utf-8")
        result = scan_library(
            self.repository,
            self.media_root,
            scanned_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(result.status, "success")
        replacement = self.repository.get_media_work_mapping(media.id)
        assert replacement is not None
        self.assertEqual(replacement.source, "deterministic")
        self.assertIsNotNone(self.repository.get_primary_library_entry_work(entry.id))

    def test_malformed_edit_preserves_previous_manual_mapping(self) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "01.mp4").write_bytes(b"x")
        sidecar = folder / "rpi-streamer.ini"
        sidecar.write_text(
            "[rpi-streamer]\nmal_id=1\n"
            '[work "primary"]\nmal_id=1\nlocal_episode_range=1-12\n',
            encoding="utf-8",
        )
        self._record(1, "Primary", 12)
        scan_library(self.repository, self.media_root, scanned_at=NOW)
        entry = self.repository.get_library_entry("Collection")
        assert entry is not None
        media = self.repository.list_media_files(entry.id)[0]
        previous = self.repository.get_media_work_mapping(media.id)

        sidecar.write_text("[rpi-streamer]\nunknown=yes\n", encoding="utf-8")
        result = scan_library(
            self.repository,
            self.media_root,
            scanned_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(self.repository.get_media_work_mapping(media.id), previous)

    def test_discovery_reports_missing_exact_file_without_writing_media(self) -> None:
        folder = self.media_root / "Collection"
        folder.mkdir()
        (folder / "01.mp4").write_bytes(b"x")
        (folder / "rpi-streamer.ini").write_text(
            '[rpi-streamer]\nmal_id=1\n[media "missing"]\nfile=no.mp4\nmal_id=1\n',
            encoding="utf-8",
        )

        result = discover(self.media_root)

        self.assertEqual(len(result.issues), 1)
        self.assertIn("does not exist", result.issues[0].message)


if __name__ == "__main__":
    unittest.main()
