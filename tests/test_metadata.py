from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rpi_streamer.database import (
    CatalogueRepository,
    ProviderEpisode,
    Relation,
)
from rpi_streamer.metadata import (
    MAX_ARTWORK_BYTES,
    AnimeCandidate,
    AnimeDetails,
    CacheValidators,
    DetailsResult,
    HttpRequest,
    HttpResponse,
    JikanProvider,
    ProviderError,
    enrich_catalogue,
    match_candidate,
    normalize_title,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def json_response(
    payload: object,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status,
        headers or {},
        json.dumps(payload).encode(),
    )


def detail_payload() -> dict[str, object]:
    return {
        "data": {
            "mal_id": 1,
            "title": "Cowboy Bebop",
            "title_english": "Cowboy Bebop",
            "title_japanese": "カウボーイビバップ",
            "title_synonyms": ["Bebop"],
            "synopsis": "Bounty hunters in space.",
            "episodes": 26,
            "genres": [{"name": "Action"}, {"name": "Sci-Fi"}],
            "relations": [
                {
                    "relation": "Side Story",
                    "entry": [
                        {
                            "mal_id": 5,
                            "type": "anime",
                            "name": "Cowboy Bebop: The Movie",
                        },
                        {"mal_id": 99, "type": "manga", "name": "Ignored"},
                    ],
                }
            ],
            "images": {"jpg": {"large_image_url": "https://images.invalid/1.jpg"}},
        }
    }


class QueueTransport:
    def __init__(self, responses: Sequence[HttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class JikanProviderTests(unittest.TestCase):
    def test_search_and_full_details_are_normalized(self) -> None:
        transport = QueueTransport(
            [
                json_response(
                    {
                        "data": [
                            {
                                "mal_id": 1,
                                "title": "Cowboy Bebop",
                                "title_english": "Cowboy Bebop",
                                "title_japanese": "カウボーイビバップ",
                                "titles": [],
                            }
                        ]
                    }
                ),
                json_response(
                    detail_payload(),
                    headers={
                        "ETag": '"detail"',
                        "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                    },
                ),
            ]
        )
        provider = JikanProvider(
            transport=transport, min_request_interval=0, sleep=lambda _: None
        )

        candidates = provider.search("Cowboy Bebop")
        details = provider.details("1").details

        self.assertEqual(candidates[0].provider_id, "1")
        self.assertIn("カウボーイビバップ", candidates[0].aliases)
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual(details.genres, ("Action", "Sci-Fi"))
        self.assertEqual(details.relations[0].target_provider_id, "5")
        self.assertEqual(details.validators.etag, '"detail"')
        self.assertIn("RPi-Streamer", transport.requests[0].headers["User-Agent"])
        self.assertEqual(transport.requests[0].timeout, 10.0)

    def test_episodes_follow_pagination(self) -> None:
        transport = QueueTransport(
            [
                json_response(
                    {
                        "data": [
                            {
                                "mal_id": 1,
                                "title": "Pilot",
                                "aired": "1998-04-03T00:00:00Z",
                                "filler": False,
                                "recap": False,
                            }
                        ],
                        "pagination": {"has_next_page": True},
                    }
                ),
                json_response(
                    {
                        "data": [
                            {
                                "mal_id": 2,
                                "title": None,
                                "aired": None,
                                "filler": True,
                                "recap": False,
                            }
                        ],
                        "pagination": {"has_next_page": False},
                    }
                ),
            ]
        )
        provider = JikanProvider(
            transport=transport, min_request_interval=0, sleep=lambda _: None
        )

        episodes = provider.episodes("1")

        self.assertEqual([episode.episode_number for episode in episodes], [1, 2])
        self.assertTrue(episodes[1].filler)
        self.assertIn("page=2", transport.requests[1].url)

    def test_conditional_304_sends_both_validators(self) -> None:
        transport = QueueTransport([HttpResponse(304, {"ETag": '"same"'}, b"")])
        provider = JikanProvider(
            transport=transport, min_request_interval=0, sleep=lambda _: None
        )

        result = provider.details(
            "1", CacheValidators('"old"', "Wed, 01 Jan 2025 00:00:00 GMT")
        )

        self.assertTrue(result.not_modified)
        self.assertEqual(transport.requests[0].headers["If-None-Match"], '"old"')
        self.assertIn("If-Modified-Since", transport.requests[0].headers)

    def test_throttle_and_retry_after_are_bounded(self) -> None:
        transport = QueueTransport(
            [
                HttpResponse(429, {"Retry-After": "2"}, b""),
                json_response({"data": []}),
            ]
        )
        sleeps: list[float] = []
        times = iter([0.0, 0.0, 3.0])
        provider = JikanProvider(
            transport=transport,
            min_request_interval=1.05,
            sleep=sleeps.append,
            clock=lambda: next(times),
        )

        self.assertEqual(provider.search("test"), [])

        self.assertEqual(len(transport.requests), 2)
        self.assertIn(2.0, sleeps)

    def test_retry_exhaustion_and_malformed_json(self) -> None:
        retrying = JikanProvider(
            transport=QueueTransport([HttpResponse(503, {}, b"")] * 3),
            min_request_interval=0,
            sleep=lambda _: None,
        )
        with self.assertRaisesRegex(ProviderError, "retry limit"):
            retrying.search("test")

        malformed = JikanProvider(
            transport=QueueTransport([HttpResponse(200, {}, b"{bad")]),
            min_request_interval=0,
            sleep=lambda _: None,
        )
        with self.assertRaisesRegex(ProviderError, "malformed JSON"):
            malformed.search("test")

    def test_artwork_rejects_oversize_and_invalid_mime(self) -> None:
        oversized = JikanProvider(
            transport=QueueTransport(
                [
                    HttpResponse(
                        200,
                        {"Content-Type": "image/jpeg"},
                        b"x" * (MAX_ARTWORK_BYTES + 1),
                    )
                ]
            ),
            min_request_interval=0,
        )
        invalid = JikanProvider(
            transport=QueueTransport(
                [HttpResponse(200, {"Content-Type": "text/html"}, b"no")]
            ),
            min_request_interval=0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "cover.jpg"
            with self.assertRaisesRegex(ProviderError, "size limit"):
                oversized.artwork("https://example.invalid/cover.jpg", destination)
            with self.assertRaisesRegex(ProviderError, "MIME"):
                invalid.artwork("https://example.invalid/cover.jpg", destination)
            self.assertFalse(destination.exists())


class MatchingTests(unittest.TestCase):
    def test_normalization_and_confident_alias_match(self) -> None:
        self.assertEqual(normalize_title("  Pokémon: S01! "), "pokemon s01")
        candidate = AnimeCandidate("1", "Kaubōi Bibappu", ("Cowboy Bebop",))
        self.assertEqual(match_candidate("Cowboy_Bebop", [candidate]), candidate)

    def test_ambiguous_and_low_confidence_matches_stay_unmatched(self) -> None:
        ambiguous = [
            AnimeCandidate("1", "Fate Stay Night", ()),
            AnimeCandidate("2", "Fate Stay Night", ()),
        ]
        self.assertIsNone(match_candidate("Fate Stay Night", ambiguous))
        self.assertIsNone(
            match_candidate(
                "Cowboy Bebop", [AnimeCandidate("3", "Completely Different", ())]
            )
        )


class FakeProvider:
    name = "jikan"

    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.detail_calls: list[tuple[str, CacheValidators | None]] = []
        self.episode_calls: list[str] = []
        self.artwork_calls: list[str] = []
        self.not_modified = False
        self.error: ProviderError | None = None
        self.artwork_error: ProviderError | None = None

    def search(self, title: str) -> Sequence[AnimeCandidate]:
        self.search_calls.append(title)
        if self.error:
            raise self.error
        return [AnimeCandidate("1", "Cowboy Bebop", ())]

    def details(
        self,
        provider_id: str,
        validators: CacheValidators | None = None,
    ) -> DetailsResult:
        self.detail_calls.append((provider_id, validators))
        if self.error:
            raise self.error
        if self.not_modified:
            return DetailsResult(None, True, CacheValidators('"same"', "last modified"))
        return DetailsResult(
            AnimeDetails(
                provider_id="1",
                title="Cowboy Bebop",
                synopsis="Bounty hunters.",
                episode_count=1,
                aliases=(("japanese", "カウボーイビバップ"),),
                genres=("Action",),
                relations=(Relation("sequel", "jikan", "5", "The Movie"),),
                artwork_url="https://images.invalid/1.jpg",
                raw_data={"mal_id": 1},
                validators=CacheValidators('"one"', "last modified"),
            )
        )

    def episodes(self, provider_id: str) -> Sequence[ProviderEpisode]:
        self.episode_calls.append(provider_id)
        return [ProviderEpisode(1, "Pilot", NOW, False, False)]

    def artwork(self, url: str, destination: Path) -> tuple[str, int]:
        self.artwork_calls.append(url)
        if self.artwork_error:
            raise self.artwork_error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"jpg")
        return "image/jpeg", 3


class EnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = CatalogueRepository(self.root / "catalogue.db")
        self.addCleanup(self.repository.close)

    def _entry(
        self,
        title: str = "Cowboy Bebop",
        *,
        metadata_enabled: bool = True,
        pinned: str | None = None,
    ) -> int:
        return self.repository.upsert_library_entry(
            relative_path=title,
            title=title,
            metadata_enabled=metadata_enabled,
            pinned_provider="jikan" if pinned else None,
            pinned_provider_id=pinned,
            seen_at=NOW,
        ).id

    def test_enrichment_persists_normalized_metadata_and_artwork(self) -> None:
        entry_id = self._entry()
        provider = FakeProvider()

        result = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=True,
            now=NOW,
        )

        self.assertEqual((result.enriched, result.errors), (1, ()))
        record = self.repository.get_provider_record(entry_id, "jikan")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(self.repository.list_genres(record.id), ["Action"])
        self.assertEqual(
            self.repository.list_provider_episodes(record.id)[0].title, "Pilot"
        )
        self.assertTrue((self.root / "artwork/jikan-1.jpg").is_file())

    def test_fresh_cache_skips_network_and_304_refreshes_stale_cache(self) -> None:
        entry_id = self._entry()
        provider = FakeProvider()
        first = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=False,
            now=NOW,
        )
        fresh = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=False,
            now=NOW + timedelta(hours=1),
        )
        provider.not_modified = True
        refreshed = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=1,
            state_dir=self.root,
            download_artwork=False,
            now=NOW + timedelta(hours=2),
        )

        self.assertEqual((first.enriched, fresh.cached, refreshed.cached), (1, 1, 1))
        self.assertEqual(len(provider.search_calls), 1)
        self.assertEqual(len(provider.detail_calls), 2)
        record = self.repository.get_provider_record(entry_id, "jikan")
        assert record is not None
        self.assertEqual(record.fetched_at, NOW + timedelta(hours=2))

    def test_pin_bypasses_search_disabled_bypasses_all_network(self) -> None:
        self._entry("Pinned", pinned="99")
        self._entry("Disabled", metadata_enabled=False)
        provider = FakeProvider()

        result = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=False,
            now=NOW,
        )

        self.assertEqual(result.disabled, 1)
        self.assertEqual(provider.search_calls, [])
        self.assertEqual(provider.detail_calls[0][0], "99")

    def test_offline_error_does_not_remove_local_or_cached_state(self) -> None:
        entry_id = self._entry()
        provider = FakeProvider()
        provider.error = ProviderError("offline")

        result = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=False,
            now=NOW,
        )

        self.assertEqual(len(result.errors), 1)
        self.assertIn("offline", result.errors[0])
        self.assertIsNotNone(self.repository.get_library_entry_by_id(entry_id))
        self.assertIsNone(self.repository.get_provider_record(entry_id, "jikan"))

    def test_artwork_failure_records_placeholder_without_losing_metadata(
        self,
    ) -> None:
        entry_id = self._entry()
        provider = FakeProvider()
        provider.artwork_error = ProviderError("image unavailable")

        result = enrich_catalogue(
            self.repository,
            provider,
            refresh_interval=86400,
            state_dir=self.root,
            download_artwork=True,
            now=NOW,
        )

        self.assertEqual(result.enriched, 1)
        self.assertIn("image unavailable", result.errors[0])
        record = self.repository.get_provider_record(entry_id, "jikan")
        assert record is not None
        artwork = self.repository.get_artwork(record.id, "cover")
        self.assertIsNotNone(artwork)
        assert artwork is not None
        self.assertIsNone(artwork.relative_path)


@unittest.skipUnless(
    os.environ.get("RPI_STREAMER_LIVE_JIKAN") == "1",
    "set RPI_STREAMER_LIVE_JIKAN=1 for the opt-in live smoke test",
)
class LiveJikanSmokeTest(unittest.TestCase):
    def test_searches_one_known_title(self) -> None:
        provider = JikanProvider()
        self.assertTrue(provider.search("Cowboy Bebop"))
