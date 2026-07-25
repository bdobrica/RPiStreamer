from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.config import Settings
from rpi_streamer.database import CatalogueRepository, ProviderEpisode, Relation
from rpi_streamer.metadata import (
    AnimeCandidate,
    AnimeDetails,
    CacheValidators,
    DetailsResult,
)
from rpi_streamer.service import run_once


class FixtureProvider:
    """Deterministic metadata boundary for the full offline pipeline."""

    name = "jikan"
    transport_name = "tenrai"

    def search(self, title: str) -> list[AnimeCandidate]:
        return [AnimeCandidate("1", title, ("Fixture Anime",))]

    def details(
        self,
        provider_id: str,
        validators: CacheValidators | None = None,
    ) -> DetailsResult:
        return DetailsResult(
            AnimeDetails(
                provider_id=provider_id,
                title="Fixture Anime",
                synopsis="Offline acceptance fixture.",
                episode_count=2,
                aliases=(),
                genres=("Adventure",),
                relations=(Relation("sequel", "jikan", "2", "Fixture Sequel"),),
                artwork_url=None,
                raw_data={"mal_id": 1},
                validators=CacheValidators('"fixture"', None),
            )
        )

    def episodes(self, provider_id: str) -> list[ProviderEpisode]:
        return [
            ProviderEpisode(1, "Arrival", None, False, False),
            ProviderEpisode(2, "Departure", None, False, False),
        ]

    def artwork(self, url: str, destination: Path) -> tuple[str, int]:
        raise AssertionError("fixture does not request artwork")


class EndToEndFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.media = root / "media"
        self.state = root / "state"
        self.site = self.state / "site"
        self.database = self.state / "catalogue.db"
        title = self.media / "Fixture Anime"
        title.mkdir(parents=True)
        self.first = title / "01 - Arrival.mp4"
        self.second = title / "02 - Departure.mp4"
        self.first.write_bytes(bytes(range(256)) * 4)
        self.second.write_bytes(b"episode two")
        self.settings = Settings(
            media_root=self.media,
            state_dir=self.state,
            site_dir=self.site,
            database_path=self.database,
            metadata_provider="tenrai",
            download_artwork=False,
        )

    @patch("rpi_streamer.service.TenraiProvider", FixtureProvider)
    def test_scan_metadata_database_generation_rescan_and_removal(self) -> None:
        first = run_once(self.settings)

        self.assertEqual(first.status, "success")
        self.assertEqual((first.discovered_entries, first.discovered_files), (1, 2))
        index = (self.site / "index.html").read_text(encoding="utf-8")
        title_page = next((self.site / "titles").glob("*.html")).read_text(
            encoding="utf-8"
        )
        self.assertIn("Fixture Anime", index)
        self.assertIn("Offline acceptance fixture.", title_page)
        self.assertIn("01%20-%20Arrival.mp4", title_page)
        self.assertIn("02%20-%20Departure.mp4", title_page)

        self.first.unlink()
        second = run_once(self.settings)
        self.assertEqual((second.status, second.discovered_files), ("success", 1))

        with CatalogueRepository(self.database) as repository:
            entry = repository.get_library_entry("Fixture Anime")
            assert entry is not None
            files = repository.list_media_files(entry.id, available_only=False)
            self.assertEqual(
                {item.relative_path: item.available for item in files},
                {
                    "Fixture Anime/01 - Arrival.mp4": False,
                    "Fixture Anime/02 - Departure.mp4": True,
                },
            )
        updated_page = next((self.site / "titles").glob("*.html")).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("01%20-%20Arrival.mp4", updated_page)
        self.assertIn("02%20-%20Departure.mp4", updated_page)
