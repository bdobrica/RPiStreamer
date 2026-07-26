from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.database import (
    CatalogueRepository,
    ProviderEpisode,
    Relation,
)
from rpi_streamer.generator import (
    GenerationError,
    generate_site,
    genre_slug,
    media_url,
    title_slug,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


class GeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.site = self.root / "published"
        self.repository = CatalogueRepository(self.state / "catalogue.db")
        self.addCleanup(self.repository.close)

    def _entry(
        self,
        path: str,
        title: str,
        *,
        filename: str = "01.mp4",
    ) -> int:
        entry = self.repository.upsert_library_entry(
            relative_path=path,
            title=title,
            seen_at=NOW,
        )
        self.repository.upsert_media_file(
            library_entry_id=entry.id,
            relative_path=f"{path}/{filename}",
            size_bytes=5,
            mtime_ns=10,
            local_identity=f"1:{entry.id}",
            seen_at=NOW,
        )
        return entry.id

    def _metadata(
        self,
        entry_id: int,
        *,
        provider_id: str = "1",
        title: str = "Cowboy Bebop",
        synopsis: str = "Bounty hunters in space.",
        genres: tuple[str, ...] = ("Action", "Sci-Fi"),
        relations: tuple[Relation, ...] = (),
        cover: bool = True,
    ) -> int:
        record = self.repository.upsert_provider_record(
            library_entry_id=entry_id,
            provider="jikan",
            provider_id=provider_id,
            canonical_title=title,
            synopsis=synopsis,
            episode_count=1,
            raw_data={"mal_id": int(provider_id)},
            fetched_at=NOW,
        )
        self.repository.replace_genres(record.id, genres)
        self.repository.replace_provider_episodes(
            record.id,
            [ProviderEpisode(1, "Asteroid Blues", NOW, False, False)],
        )
        self.repository.replace_relations(record.id, relations)
        if cover:
            cover_path = self.state / f"artwork/jikan-{provider_id}.jpg"
            cover_path.parent.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(b"jpeg")
            self.repository.upsert_artwork(
                provider_record_id=record.id,
                kind="cover",
                source_url=f"https://images.invalid/{provider_id}.jpg",
                relative_path=f"artwork/jikan-{provider_id}.jpg",
                mime_type="image/jpeg",
                size_bytes=4,
                fetched_at=NOW,
            )
        return record.id

    def _files(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_complete_catalogue_renders_navigation_metadata_and_video(self) -> None:
        movie_id = self._entry("Movie", "Cowboy Bebop: The Movie")
        self._metadata(movie_id, provider_id="5", title="The Movie", cover=False)
        series_id = self._entry(
            "Cowboy Bebop",
            "Cowboy Bebop",
            filename="01 - Asteroid Blues #1.mp4",
        )
        self._metadata(
            series_id,
            relations=(Relation("sequel", "jikan", "5", "Cowboy Bebop: The Movie"),),
        )
        scan = self.repository.start_scan(started_at=NOW)
        self.repository.finish_scan(
            scan.id,
            status="success",
            discovered_entries=2,
            discovered_files=2,
            finished_at=NOW,
        )

        result = generate_site(
            self.repository, site_dir=self.site, state_dir=self.state
        )

        entry = self.repository.get_library_entry_by_id(series_id)
        assert entry is not None
        page = (self.site / "titles" / f"{title_slug(entry)}.html").read_text()
        self.assertEqual(result.title_count, 2)
        self.assertEqual(self.site.stat().st_mode & 0o050, 0o050)
        self.assertIn('<video controls preload="metadata"', page)
        self.assertIn(
            "/media/Cowboy%20Bebop/01%20-%20Asteroid%20Blues%20%231.mp4", page
        )
        self.assertIn("Provider episode context", page)
        self.assertIn("Asteroid Blues", page)
        self.assertIn("Related titles", page)
        self.assertIn(f"title-{movie_id:08x}.html", page)
        self.assertIn("Last scan:", page)
        self.assertEqual(
            len(tuple((self.site / "assets/covers").glob("jikan-1-*.jpg"))),
            1,
        )
        self.assertEqual(len(tuple((self.site / "assets").glob("style-*.css"))), 1)
        self.assertEqual(
            len(tuple((self.site / "assets").glob("episode-player-*.js"))), 1
        )
        self.assertTrue(
            (self.site / "genres" / f"{genre_slug('Sci-Fi')}.html").is_file()
        )

    def test_unmatched_unicode_and_missing_art_have_safe_fallbacks(self) -> None:
        entry_id = self._entry("日本語 & more", "日本語 <Anime>")

        generate_site(self.repository, site_dir=self.site, state_dir=self.state)

        entry = self.repository.get_library_entry_by_id(entry_id)
        assert entry is not None
        page = (self.site / "titles" / f"{title_slug(entry)}.html").read_text()
        self.assertIn("Unmatched local title", page)
        self.assertIn("日本語 &lt;Anime&gt;", page)
        self.assertIn('aria-label="No cover art"', page)
        self.assertIn("/media/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%26%20more/01.mp4", page)

    def test_all_untrusted_text_is_escaped_and_remote_art_url_is_not_rendered(
        self,
    ) -> None:
        entry_id = self._entry(
            "Unsafe",
            '<img src=x onerror="bad()">',
            filename='"><script>alert(1)</script>.mp4',
        )
        record_id = self._metadata(
            entry_id,
            title="Unsafe",
            synopsis="<script>remote()</script>",
            genres=("<svg onload=bad()>",),
            relations=(Relation("sequel", "jikan", "99", "<b>Related</b>"),),
            cover=False,
        )
        self.repository.upsert_artwork(
            provider_record_id=record_id,
            kind="cover",
            source_url='javascript:alert("bad")',
        )

        generate_site(self.repository, site_dir=self.site, state_dir=self.state)

        output = "\n".join(
            data.decode("utf-8", errors="ignore")
            for name, data in self._files(self.site).items()
            if name.endswith(".html")
        )
        self.assertNotIn("<script>", output)
        self.assertNotIn("<svg", output)
        self.assertNotIn("javascript:", output)
        self.assertIn("&lt;script&gt;remote()&lt;/script&gt;", output)
        self.assertIn("&lt;b&gt;Related&lt;/b&gt;", output)

    def test_same_display_names_have_distinct_stable_slugs(self) -> None:
        first_id = self._entry("One", "Same")
        second_id = self._entry("Two", "Same")
        first = self.repository.get_library_entry_by_id(first_id)
        second = self.repository.get_library_entry_by_id(second_id)
        assert first is not None and second is not None

        self.assertNotEqual(title_slug(first), title_slug(second))
        self.assertEqual(title_slug(first), title_slug(first))

    def test_unchanged_catalogue_generates_identical_output(self) -> None:
        entry_id = self._entry("Stable", "Stable")
        self._metadata(entry_id)
        generate_site(self.repository, site_dir=self.site, state_dir=self.state)
        first = self._files(self.site)

        result = generate_site(
            self.repository, site_dir=self.site, state_dir=self.state
        )

        self.assertEqual(self._files(self.site), first)
        self.assertEqual(result.previous_dir, self.root / "published.previous")
        assert result.previous_dir is not None
        self.assertEqual(self._files(result.previous_dir), first)

    def test_failed_render_keeps_previous_published_site(self) -> None:
        self._entry("Stable", "Stable")
        generate_site(self.repository, site_dir=self.site, state_dir=self.state)
        before = self._files(self.site)

        with (
            patch(
                "rpi_streamer.generator._validate",
                side_effect=GenerationError("broken output"),
            ),
            self.assertRaisesRegex(GenerationError, "broken output"),
        ):
            generate_site(self.repository, site_dir=self.site, state_dir=self.state)

        self.assertEqual(self._files(self.site), before)
        self.assertEqual(list(self.root.glob(".published.staging-*")), [])

    def test_pages_are_semantic_keyboard_accessible_and_javascript_free(self) -> None:
        self._entry("Accessible", "Accessible")
        generate_site(self.repository, site_dir=self.site, state_dir=self.state)
        page = (self.site / "index.html").read_text()

        self.assertIn('<a class="skip-link" href="#content">', page)
        self.assertIn('<nav aria-label="Primary">', page)
        self.assertIn('<main id="content">', page)
        self.assertIn("<h1>Anime titles</h1>", page)
        self.assertNotIn("<script", page.casefold())

    def test_many_local_episodes_render_one_player_and_navigation(self) -> None:
        entry_id = self._entry("Series", "Series", filename="01.mp4")
        self.repository.upsert_media_file(
            library_entry_id=entry_id,
            relative_path="Series/02 & finale.mp4",
            size_bytes=5,
            mtime_ns=11,
            local_identity="1:finale",
            seen_at=NOW,
        )

        generate_site(self.repository, site_dir=self.site, state_dir=self.state)

        entry = self.repository.get_library_entry_by_id(entry_id)
        assert entry is not None
        page = (self.site / "titles" / f"{title_slug(entry)}.html").read_text()
        self.assertEqual(page.count("<video "), 1)
        self.assertIn("data-episode-select", page)
        self.assertIn("data-episode-previous", page)
        self.assertIn("data-episode-next", page)
        self.assertIn("02 &amp; finale.mp4", page)
        self.assertIn("02%20%26%20finale.mp4", page)
        self.assertIn("<noscript>", page)
        self.assertRegex(page, r"assets/episode-player-[0-9a-f]{16}\.js")

    def test_multi_work_collection_groups_media_and_context(self) -> None:
        entry_id = self._entry("MF Ghost", "MF Ghost", filename="S01E01.mp4")
        primary_record_id = self._metadata(
            entry_id, provider_id="50695", title="MF Ghost"
        )
        entry = self.repository.get_library_entry_by_id(entry_id)
        assert entry is not None
        primary_work = self.repository.get_primary_library_entry_work(entry_id)
        assert primary_work is not None
        season_two_record = self.repository.upsert_detached_provider_record(
            provider="jikan",
            provider_id="57559",
            canonical_title="MF Ghost 2nd Season",
            synopsis="The second season.",
            episode_count=12,
            raw_data={"mal_id": 57559, "type": "TV"},
            fetched_at=NOW,
        )
        self.repository.replace_provider_episodes(
            season_two_record.id,
            [ProviderEpisode(1, "The White Reaper", NOW, False, False)],
        )
        season_two = self.repository.associate_library_entry_work(
            library_entry_id=entry_id,
            provider_record_id=season_two_record.id,
            local_name="season-2",
            source="relation",
            verified_at=NOW,
            label="Season 2",
            display_order=1100,
            confidence=0.96,
            relation_distance=1,
        )
        self.repository.replace_relations(
            primary_record_id,
            [Relation("sequel", "jikan", "57559", "MF Ghost 2nd Season")],
        )
        season_two_file = self.repository.upsert_media_file(
            library_entry_id=entry_id,
            relative_path="MF Ghost/S02E01 & start.mp4",
            size_bytes=5,
            mtime_ns=11,
            local_identity="1:s2",
            seen_at=NOW,
        )
        unmapped = self.repository.upsert_media_file(
            library_entry_id=entry_id,
            relative_path="MF Ghost/Bonus <unknown>.mp4",
            size_bytes=5,
            mtime_ns=12,
            local_identity="1:bonus",
            seen_at=NOW,
        )
        season_one_file = next(
            media
            for media in self.repository.list_media_files(entry_id)
            if media.filename == "S01E01.mp4"
        )
        self.repository.set_media_work_mapping(
            media_file_id=season_one_file.id,
            library_entry_work_id=primary_work.id,
            kind="episode",
            episode_start=1,
            episode_end=1,
            source="deterministic",
            schema_version="test",
            input_digest="s1",
            mapped_at=NOW,
        )
        self.repository.set_media_work_mapping(
            media_file_id=season_two_file.id,
            library_entry_work_id=season_two.id,
            kind="episode",
            episode_start=1,
            episode_end=1,
            source="model",
            confidence=0.91,
            model="gpt-5.6-luna",
            schema_version="test",
            input_digest="s2",
            mapped_at=NOW,
        )

        generate_site(self.repository, site_dir=self.site, state_dir=self.state)

        page = (self.site / "titles" / f"{title_slug(entry)}.html").read_text(
            encoding="utf-8"
        )
        self.assertEqual(page.count("<video "), 1)
        self.assertEqual(page.count("<optgroup "), 3)
        self.assertLess(
            page.index('<optgroup label="MF Ghost">'),
            page.index('<optgroup label="MF Ghost 2nd Season">'),
        )
        self.assertLess(
            page.index('<optgroup label="MF Ghost 2nd Season">'),
            page.index('<optgroup label="Unmapped">'),
        )
        self.assertIn("Episode 1 — S01E01.mp4", page)
        self.assertIn("Episode 1 — S02E01 &amp; start.mp4", page)
        self.assertIn("Bonus &lt;unknown&gt;.mp4", page)
        self.assertIn("data-work-heading", page)
        self.assertIn('data-episode-title="The White Reaper"', page)
        self.assertIn("Works in this collection", page)
        self.assertIn("Season 2", page)
        self.assertIn("Relation (96%)", page)
        self.assertIn("<h3>MF Ghost 2nd Season</h3>", page)
        self.assertIn("The White Reaper", page)
        self.assertIn(
            "/media/MF%20Ghost/S02E01%20%26%20start.mp4",
            page,
        )
        self.assertIn("<noscript>", page)
        self.assertIn(
            "/media/MF%20Ghost/Bonus%20%3Cunknown%3E.mp4",
            page,
        )
        self.assertIsNotNone(unmapped)

    def test_episode_script_navigates_flat_options_across_groups(self) -> None:
        script = (
            Path(__file__).parents[1]
            / "src"
            / "rpi_streamer"
            / "static"
            / "episode-player.js"
        ).read_text(encoding="utf-8")

        self.assertIn("Array.from(selector.options)", script)
        self.assertIn("selector.selectedIndex - 1", script)
        self.assertIn("selector.selectedIndex + 1", script)
        self.assertIn("#episode-", script)


class UrlAndSlugTests(unittest.TestCase):
    def test_media_url_encodes_special_and_unicode_characters(self) -> None:
        self.assertEqual(
            media_url("A #1/日本語 & test.mp4"),
            "/media/A%20%231/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%26%20test.mp4",
        )

    def test_media_url_rejects_traversal_and_noncanonical_paths(self) -> None:
        for path in ("../secret.mp4", "A/../secret.mp4", "/absolute.mp4", "A//1.mp4"):
            with self.subTest(path=path), self.assertRaises(GenerationError):
                media_url(path)

    def test_genre_slug_resists_normalization_collisions(self) -> None:
        self.assertNotEqual(genre_slug("Sci Fi"), genre_slug("Sci-Fi"))
