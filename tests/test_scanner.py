from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.database import CatalogueRepository
from rpi_streamer.scanner import discover, episode_hint, natural_key, scan_library

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "anime"
        self.root.mkdir()
        self.repository = CatalogueRepository(
            Path(self.temporary.name) / "state" / "catalogue.db"
        )
        self.addCleanup(self.repository.close)

    def _media(self, relative_path: str, content: bytes = b"video") -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_discovers_nested_unicode_and_natural_sorts_case_insensitively(
        self,
    ) -> None:
        self._media("Shows/JoJo #1/10 finale.MP4")
        self._media("Shows/JoJo #1/2 start.mp4")
        self._media("日本語/S01E02 - 二.mp4")

        result = discover(self.root)

        self.assertEqual(
            [title.relative_path for title in result.titles],
            [
                "Shows/JoJo #1",
                "日本語",
            ],
        )
        self.assertEqual(
            [item.filename for item in result.titles[0].files],
            ["2 start.mp4", "10 finale.MP4"],
        )
        self.assertEqual(result.titles[1].files[0].episode_hint, "S01E02")
        self.assertEqual(result.issues, ())

    def test_sidecar_overrides_title_and_metadata_pin(self) -> None:
        self._media("show_name/01.mp4")
        (self.root / "show_name/rpi-streamer.ini").write_text(
            "[rpi-streamer]\n"
            "display_title = A Better Name\n"
            "sort_title = Better Name, A\n"
            "metadata_enabled = no\n"
            "mal_id = 123\n",
            encoding="utf-8",
        )

        title = discover(self.root).titles[0]

        self.assertEqual(title.title, "A Better Name")
        self.assertEqual(title.sort_title, "Better Name, A")
        self.assertFalse(title.metadata_enabled)
        self.assertEqual(
            (title.pinned_provider, title.pinned_provider_id),
            (
                "jikan",
                "123",
            ),
        )

    def test_malformed_sidecar_is_partial_and_uses_safe_defaults(self) -> None:
        self._media("Bad.Sidecar/01.mp4")
        (self.root / "Bad.Sidecar/rpi-streamer.ini").write_text(
            "[wrong]\nmal_id = nope\n", encoding="utf-8"
        )

        result = scan_library(self.repository, self.root, scanned_at=NOW)

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error_count, 1)
        self.assertEqual(self.repository.list_library_entries()[0].title, "Bad Sidecar")

    def test_external_file_and_directory_symlinks_are_not_catalogued(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        external_file = outside / "outside.mp4"
        external_file.write_bytes(b"x")
        (outside / "Hidden").mkdir()
        (outside / "Hidden/01.mp4").write_bytes(b"x")
        title = self.root / "Links"
        title.mkdir()
        os.symlink(external_file, title / "escape.mp4")
        os.symlink(outside / "Hidden", self.root / "linked-title")
        self._media("Links/01.mp4")

        result = discover(self.root)

        self.assertEqual(len(result.titles), 1)
        self.assertEqual([item.filename for item in result.titles[0].files], ["01.mp4"])
        self.assertEqual(len(result.issues), 1)
        self.assertIn("escapes", result.issues[0].message)

    def test_unreadable_directory_is_reported(self) -> None:
        def inaccessible_walk(
            _root: Path,
            *,
            topdown: bool,
            onerror: object,
            followlinks: bool,
        ) -> list[tuple[str, list[str], list[str]]]:
            self.assertTrue(topdown)
            self.assertFalse(followlinks)
            assert callable(onerror)
            onerror(PermissionError(13, "Permission denied", str(self.root / "Nope")))
            return []

        with patch("rpi_streamer.scanner.os.walk", inaccessible_walk):
            result = discover(self.root)

        self.assertEqual(result.titles, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].path, "Nope")
        self.assertIn("Permission denied", result.issues[0].message)

    def test_successful_scans_reconcile_change_move_and_remove(self) -> None:
        first = self._media("Old Name/01.mp4", b"one")
        initial = scan_library(self.repository, self.root, scanned_at=NOW)
        old_entry = self.repository.list_library_entries()[0]
        old_file = self.repository.list_media_files(old_entry.id)[0]

        destination = self.root / "New Name/02.mp4"
        destination.parent.mkdir()
        first.rename(destination)
        destination.write_bytes(b"changed")
        second_time = NOW + timedelta(seconds=1)
        changed = scan_library(self.repository, self.root, scanned_at=second_time)

        entries = self.repository.list_library_entries()
        self.assertEqual((initial.status, changed.status), ("success", "success"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, old_entry.id)
        self.assertEqual(entries[0].relative_path, "New Name")
        media = self.repository.list_media_files(entries[0].id)
        self.assertEqual(media[0].id, old_file.id)
        self.assertEqual(media[0].relative_path, "New Name/02.mp4")
        self.assertEqual(media[0].size_bytes, 7)

        destination.unlink()
        scan_library(
            self.repository, self.root, scanned_at=second_time + timedelta(seconds=1)
        )
        self.assertEqual(self.repository.list_library_entries(), [])
        self.assertFalse(
            self.repository.list_library_entries(available_only=False)[0].available
        )
        self.assertFalse(
            self.repository.list_media_files(old_entry.id, available_only=False)[
                0
            ].available
        )

    def test_two_unchanged_scans_are_idempotent(self) -> None:
        self._media("Stable/02.mp4")
        self._media("Stable/01.mp4")
        first = scan_library(self.repository, self.root, scanned_at=NOW)
        entry_before = self.repository.list_library_entries()[0]
        files_before = self.repository.list_media_files(entry_before.id)

        second = scan_library(self.repository, self.root, scanned_at=NOW)

        self.assertEqual((first.status, second.status), ("success", "success"))
        self.assertEqual(self.repository.list_library_entries()[0], entry_before)
        self.assertEqual(
            self.repository.list_media_files(entry_before.id), files_before
        )

    def test_partial_scan_does_not_mark_previous_rows_unavailable(self) -> None:
        self._media("Good/01.mp4")
        scan_library(self.repository, self.root, scanned_at=NOW)
        (self.root / "Good/01.mp4").unlink()
        self._media("Broken/01.mp4")
        (self.root / "Broken/rpi-streamer.ini").write_text(
            "not an ini", encoding="utf-8"
        )

        result = scan_library(
            self.repository, self.root, scanned_at=NOW + timedelta(seconds=1)
        )

        self.assertEqual(result.status, "partial")
        entry = self.repository.get_library_entry("Good")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(entry.available)

    def test_metadata_errors_are_included_in_scan_summary(self) -> None:
        self._media("Offline/01.mp4")

        result = scan_library(
            self.repository,
            self.root,
            scanned_at=NOW,
            enrich=lambda _repository, _timestamp: ("Offline: provider down",),
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error_count, 1)
        self.assertIn("provider down", result.summary or "")
        self.assertTrue(self.repository.get_library_entry("Offline"))

    def test_scan_does_not_write_to_media_root(self) -> None:
        self._media("Read Only/01.mp4")
        before = {
            path.relative_to(self.root): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
        }

        scan_library(self.repository, self.root, scanned_at=NOW)

        after = {
            path.relative_to(self.root): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
        }
        self.assertEqual(after, before)


class EpisodeHintTests(unittest.TestCase):
    def test_common_episode_forms(self) -> None:
        cases = {
            "01 - Pilot.mp4": "01",
            "S2E3 title.mp4": "S02E03",
            "S01E01-03.mp4": "S01E01-E03",
            "OVA 2.mp4": "OVA 2",
            "Special.mp4": "Special",
            "Movie 2.mp4": None,
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(episode_hint(filename), expected)

    def test_natural_key(self) -> None:
        self.assertEqual(
            sorted(["10.mp4", "2.mp4", "1.mp4"], key=natural_key),
            ["1.mp4", "2.mp4", "10.mp4"],
        )
