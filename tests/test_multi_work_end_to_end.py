from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import patch

from rpi_streamer.config import Settings
from rpi_streamer.database import (
    CatalogueRepository,
    ProviderEpisode,
    Relation,
)
from rpi_streamer.metadata import (
    AnimeCandidate,
    AnimeDetails,
    CacheValidators,
    DetailsResult,
)
from rpi_streamer.service import run_once

FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"


def _load_fixture(name: str) -> dict[str, Any]:
    payload: object = json.loads(
        (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"{name} fixture must contain an object")
    return cast(dict[str, Any], payload)


def _expand_files(fixture: dict[str, Any]) -> list[str]:
    files = list(fixture.get("files", []))
    for series in fixture.get("file_series", []):
        files.extend(
            series["template"].format(episode=episode)
            for episode in range(series["start"], series["end"] + 1)
        )
    return files


class MultiWorkFixtureProvider:
    """Offline Tenrai-compatible boundary shared by the full pipeline."""

    name = "jikan"
    transport_name = "tenrai"
    details_calls: ClassVar[list[str]] = []
    episode_calls: ClassVar[list[str]] = []
    definitions: ClassVar[dict[str, dict[str, Any]]] = {}
    relations: ClassVar[dict[str, tuple[Relation, ...]]] = {}

    def search(self, title: str) -> list[AnimeCandidate]:
        raise AssertionError(f"pinned acceptance fixture searched for {title}")

    def details(
        self,
        provider_id: str,
        validators: CacheValidators | None = None,
    ) -> DetailsResult:
        self.details_calls.append(provider_id)
        definition = self.definitions[provider_id]
        return DetailsResult(
            AnimeDetails(
                provider_id=provider_id,
                title=definition["title"],
                synopsis=f"Offline metadata for {definition['title']}.",
                episode_count=definition["episode_count"],
                aliases=(),
                genres=("Acceptance",),
                relations=self.relations.get(provider_id, ()),
                artwork_url=None,
                raw_data={"mal_id": int(provider_id)},
                validators=CacheValidators(f'"fixture-{provider_id}"', None),
            )
        )

    def episodes(self, provider_id: str) -> list[ProviderEpisode]:
        self.episode_calls.append(provider_id)
        count = int(self.definitions[provider_id]["episode_count"])
        return [
            ProviderEpisode(number, f"Provider episode {number}", None, False, False)
            for number in range(1, count + 1)
        ]

    def artwork(self, url: str, destination: Path) -> tuple[str, int]:
        raise AssertionError("acceptance fixtures do not request artwork")


class MultiWorkEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.media = root / "media"
        self.state = root / "state"
        self.site = self.state / "site"
        self.database = self.state / "catalogue.db"
        self.fixtures = {
            name: _load_fixture(name)
            for name in ("mf_ghost_continuous", "tsukimichi_reset")
        }
        definitions: dict[str, dict[str, Any]] = {}
        relations: dict[str, tuple[Relation, ...]] = {}
        for fixture in self.fixtures.values():
            works = fixture["works"]
            for index, work in enumerate(works):
                provider_id = str(work["mal_id"])
                definitions[provider_id] = work
                if index + 1 < len(works):
                    target = works[index + 1]
                    relations[provider_id] = (
                        Relation(
                            "sequel",
                            "jikan",
                            str(target["mal_id"]),
                            target["title"],
                        ),
                    )
        MultiWorkFixtureProvider.definitions = definitions
        MultiWorkFixtureProvider.relations = relations
        MultiWorkFixtureProvider.details_calls = []
        MultiWorkFixtureProvider.episode_calls = []

        for name, fixture in self.fixtures.items():
            collection = self.media / name
            collection.mkdir(parents=True)
            shutil.copyfile(FIXTURES / f"{name}.ini", collection / "rpi-streamer.ini")
            for filename in _expand_files(fixture):
                (collection / filename).write_bytes(b"synthetic mp4 fixture")

        self.settings = Settings(
            media_root=self.media,
            state_dir=self.state,
            site_dir=self.site,
            database_path=self.database,
            metadata_provider="tenrai",
            download_artwork=False,
            openai_fallback_enabled=False,
        )

    @patch("rpi_streamer.service.TenraiProvider", MultiWorkFixtureProvider)
    def test_both_layouts_cold_cached_rename_removal_and_no_javascript(
        self,
    ) -> None:
        cold = run_once(self.settings)

        self.assertEqual(cold.status, "success")
        self.assertEqual((cold.discovered_entries, cold.discovered_files), (2, 74))
        self.assertEqual(
            set(MultiWorkFixtureProvider.details_calls),
            set(MultiWorkFixtureProvider.definitions),
        )
        self.assertEqual(
            set(MultiWorkFixtureProvider.episode_calls),
            set(MultiWorkFixtureProvider.definitions),
        )
        with CatalogueRepository(self.database) as repository:
            mf_entry = repository.get_library_entry("mf_ghost_continuous")
            tsuki_entry = repository.get_library_entry("tsukimichi_reset")
            assert mf_entry is not None and tsuki_entry is not None
            self.assertEqual(len(repository.list_library_entry_works(mf_entry.id)), 3)
            self.assertEqual(
                len(repository.list_library_entry_works(tsuki_entry.id)), 2
            )
            self.assertEqual(len(repository.list_media_work_mappings(mf_entry.id)), 37)
            self.assertEqual(
                len(repository.list_media_work_mappings(tsuki_entry.id)), 37
            )
            pages = {
                entry.relative_path: (
                    self.site / "titles" / f"title-{entry.id:08x}.html"
                ).read_text(encoding="utf-8")
                for entry in (mf_entry, tsuki_entry)
            }

        mf_html = pages["mf_ghost_continuous"]
        tsuki_html = pages["tsukimichi_reset"]
        self.assertEqual(mf_html.count("<optgroup label="), 3)
        self.assertEqual(tsuki_html.count("<optgroup label="), 2)
        self.assertEqual(mf_html.count("<video "), 1)
        self.assertEqual(tsuki_html.count("<video "), 1)
        self.assertIn("<noscript>", mf_html)
        self.assertEqual(mf_html.count('<li><a href="/media/'), 37)
        self.assertEqual(tsuki_html.count('<li><a href="/media/'), 37)
        self.assertIn(
            'data-work-title="MF Ghost Fixture Season 3" '
            'data-episode-title="Provider episode 13">Episode 13',
            mf_html,
        )
        self.assertIn(
            'data-work-title="Tsukimichi Fixture Season 2" '
            'data-episode-title="Provider episode 25">Episode 25',
            tsuki_html,
        )
        self.assertIn("Provider episode 13", mf_html)
        self.assertIn("Provider episode 25", tsuki_html)

        MultiWorkFixtureProvider.details_calls.clear()
        MultiWorkFixtureProvider.episode_calls.clear()
        cached = run_once(self.settings)
        self.assertEqual(cached.status, "success")
        self.assertEqual(MultiWorkFixtureProvider.details_calls, [])
        self.assertEqual(MultiWorkFixtureProvider.episode_calls, [])
        offline = run_once(replace(self.settings, metadata_provider="none"))
        self.assertEqual(offline.status, "success")
        self.assertEqual(offline.discovered_files, 74)

        collection = self.media / "tsukimichi_reset"
        original = collection / "Fixture_Tsukimichi_2nd_Season_-_25_1080p.mp4"
        renamed = collection / "Fixture_Tsukimichi_2nd_Season_-_25_Remux.mp4"
        original.rename(renamed)
        removed = collection / "Fixture_Tsukimichi_-_12_1080p.mp4"
        removed.unlink()

        changed = run_once(self.settings)
        self.assertEqual(changed.status, "success")
        self.assertEqual(changed.discovered_files, 73)
        with CatalogueRepository(self.database) as repository:
            entry = repository.get_library_entry("tsukimichi_reset")
            assert entry is not None
            files = repository.list_media_files(entry.id, available_only=False)
            states = {item.filename: item.available for item in files}
            self.assertFalse(states[removed.name])
            self.assertTrue(states[renamed.name])
            self.assertNotIn(original.name, states)
            mappings = repository.list_media_work_mappings(entry.id)
            media_by_id = {media.id: media for media in files}
            mapped_names = {
                media_by_id[mapping.media_file_id].filename
                for mapping in mappings
                if mapping.media_file_id in media_by_id
            }
            self.assertIn(renamed.name, mapped_names)
            self.assertNotIn(removed.name, mapped_names)
            page = (self.site / "titles" / f"title-{entry.id:08x}.html").read_text(
                encoding="utf-8"
            )
        self.assertIn("25_Remux.mp4", page)
        self.assertNotIn(removed.name, page)


if __name__ == "__main__":
    unittest.main()
