"""Offline-tolerant anime metadata providers, matching, and enrichment."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Final, Protocol

from rpi_streamer.database import (
    CatalogueRepository,
    LibraryEntry,
    ProviderEpisode,
    ProviderRecord,
    Relation,
)

JIKAN_BASE_URL: Final = "https://api.jikan.moe/v4"
USER_AGENT: Final = "RPi-Streamer/0.1 (+https://github.com/bdobrica/RPiStreamer)"
MIN_REQUEST_INTERVAL: Final = 1.05
DEFAULT_TIMEOUT: Final = 10.0
DEFAULT_MAX_ATTEMPTS: Final = 3
MAX_JSON_BYTES: Final = 4 * 1024 * 1024
MAX_ARTWORK_BYTES: Final = 5 * 1024 * 1024
MATCH_THRESHOLD: Final = 0.88
MATCH_MARGIN: Final = 0.08
_TRANSIENT_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
_IMAGE_TYPES: Final = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_WORD_RE: Final = re.compile(r"[a-z0-9]+")


class ProviderError(RuntimeError):
    """A bounded, user-reportable provider or payload failure."""


@dataclass(frozen=True, slots=True)
class CacheValidators:
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class AnimeCandidate:
    provider_id: str
    title: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnimeDetails:
    provider_id: str
    title: str
    synopsis: str | None
    episode_count: int | None
    aliases: tuple[tuple[str, str], ...]
    genres: tuple[str, ...]
    relations: tuple[Relation, ...]
    artwork_url: str | None
    raw_data: object
    validators: CacheValidators


@dataclass(frozen=True, slots=True)
class DetailsResult:
    details: AnimeDetails | None
    not_modified: bool = False
    validators: CacheValidators = CacheValidators()


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str]
    timeout: float
    max_bytes: int


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class AnimeProvider(Protocol):
    """Provider boundary used by catalogue enrichment."""

    name: str

    def search(self, title: str) -> Sequence[AnimeCandidate]: ...

    def details(
        self,
        provider_id: str,
        validators: CacheValidators | None = None,
    ) -> DetailsResult: ...

    def episodes(self, provider_id: str) -> Sequence[ProviderEpisode]: ...

    def artwork(self, url: str, destination: Path) -> tuple[str, int]: ...


Transport = Callable[[HttpRequest], HttpResponse]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


class JikanProvider:
    """Small synchronous Jikan v4 client with global per-instance throttling."""

    name = "jikan"

    def __init__(
        self,
        *,
        base_url: str = JIKAN_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        min_request_interval: float = MIN_REQUEST_INTERVAL,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        transport: Transport | None = None,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        if timeout <= 0 or min_request_interval < 0 or max_attempts <= 0:
            raise ValueError("invalid Jikan timeout, interval, or attempt count")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self.max_attempts = max_attempts
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def search(self, title: str) -> Sequence[AnimeCandidate]:
        query = urllib.parse.urlencode({"q": title, "limit": 10, "sfw": "true"})
        payload, _ = self._json_request(f"/anime?{query}")
        data = _list_field(payload, "data")
        candidates: list[AnimeCandidate] = []
        for item in data:
            record = _mapping(item, "search result")
            provider_id = str(_positive_int(record.get("mal_id"), "mal_id"))
            canonical = _text(record.get("title"), "title")
            aliases = _candidate_aliases(record)
            candidates.append(AnimeCandidate(provider_id, canonical, aliases))
        return candidates

    def details(
        self,
        provider_id: str,
        validators: CacheValidators | None = None,
    ) -> DetailsResult:
        headers = _conditional_headers(validators)
        payload, response = self._json_request(
            f"/anime/{_provider_id(provider_id)}/full",
            headers=headers,
            allow_not_modified=True,
        )
        response_validators = CacheValidators(
            _header(response.headers, "etag"),
            _header(response.headers, "last-modified"),
        )
        if response.status == 304:
            return DetailsResult(
                None, not_modified=True, validators=response_validators
            )
        record = _mapping(payload.get("data"), "anime details")
        details = AnimeDetails(
            provider_id=str(_positive_int(record.get("mal_id"), "mal_id")),
            title=_text(record.get("title"), "title"),
            synopsis=_optional_text(record.get("synopsis")),
            episode_count=_optional_nonnegative_int(record.get("episodes"), "episodes"),
            aliases=_detail_aliases(record),
            genres=tuple(_named_values(record.get("genres"), "genres")),
            relations=tuple(_relations(record.get("relations"))),
            artwork_url=_artwork_url(record.get("images")),
            raw_data=record,
            validators=response_validators,
        )
        return DetailsResult(details, validators=response_validators)

    def episodes(self, provider_id: str) -> Sequence[ProviderEpisode]:
        identifier = _provider_id(provider_id)
        page = 1
        episodes: list[ProviderEpisode] = []
        while True:
            payload, _ = self._json_request(f"/anime/{identifier}/episodes?page={page}")
            for item in _list_field(payload, "data"):
                record = _mapping(item, "episode")
                number = _positive_int(record.get("mal_id"), "episode mal_id")
                episodes.append(
                    ProviderEpisode(
                        episode_number=number,
                        title=_optional_text(record.get("title")),
                        aired_at=_optional_datetime(record.get("aired")),
                        filler=bool(record.get("filler", False)),
                        recap=bool(record.get("recap", False)),
                    )
                )
            pagination = _mapping(payload.get("pagination", {}), "pagination")
            if not bool(pagination.get("has_next_page", False)):
                break
            page += 1
        return episodes

    def artwork(self, url: str, destination: Path) -> tuple[str, int]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderError("artwork URL must be absolute HTTP(S)")
        response = self._request(
            url,
            headers={"Accept": "image/*"},
            max_bytes=MAX_ARTWORK_BYTES,
        )
        if response.status != 200:
            raise ProviderError(f"artwork request returned HTTP {response.status}")
        mime_type = (
            (_header(response.headers, "content-type") or "")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if mime_type not in _IMAGE_TYPES:
            raise ProviderError(f"unsupported artwork MIME type: {mime_type or 'none'}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(response.body)
        temporary.replace(destination)
        return mime_type, len(response.body)

    def _json_request(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[Mapping[str, object], HttpResponse]:
        response = self._request(
            f"{self.base_url}{path}",
            headers=headers,
            max_bytes=MAX_JSON_BYTES,
            allow_not_modified=allow_not_modified,
        )
        if response.status == 304:
            return {}, response
        if response.status != 200:
            raise ProviderError(f"Jikan returned HTTP {response.status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProviderError(f"Jikan returned malformed JSON: {error}") from error
        return _mapping(payload, "response"), response

    def _request(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int,
        allow_not_modified: bool = False,
    ) -> HttpResponse:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                response = self._transport(
                    HttpRequest(url, request_headers, self.timeout, max_bytes)
                )
            except (OSError, TimeoutError) as error:
                if attempt + 1 == self.max_attempts:
                    raise ProviderError(f"Jikan request failed: {error}") from error
                self._sleep(2**attempt)
                continue
            content_length = _header(response.headers, "content-length")
            try:
                declared_size = None if content_length is None else int(content_length)
            except ValueError as error:
                raise ProviderError("invalid response Content-Length") from error
            if len(response.body) > max_bytes or (
                declared_size is not None and declared_size > max_bytes
            ):
                raise ProviderError("response exceeds configured size limit")
            if response.status == 304 and allow_not_modified:
                return response
            if response.status not in _TRANSIENT_STATUSES:
                return response
            if attempt + 1 == self.max_attempts:
                raise ProviderError(
                    f"Jikan retry limit reached after HTTP {response.status}"
                )
            retry_after = _retry_after(response.headers)
            self._sleep(max(float(2**attempt), retry_after))
        raise AssertionError("request retry loop did not return")

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    enriched: int
    cached: int
    unmatched: int
    disabled: int
    errors: tuple[str, ...]


def normalize_title(value: str) -> str:
    """Normalize a title deterministically for local candidate matching."""

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    asciiish = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_WORD_RE.findall(asciiish))


def match_candidate(
    title: str,
    candidates: Sequence[AnimeCandidate],
    *,
    threshold: float = MATCH_THRESHOLD,
    margin: float = MATCH_MARGIN,
) -> AnimeCandidate | None:
    """Return one high-confidence, clearly separated candidate."""

    target = normalize_title(title)
    scored = sorted(
        (
            (
                max(
                    _title_score(target, normalize_title(name)) for name in _names(item)
                ),
                item.provider_id,
                item,
            )
            for item in candidates
        ),
        key=lambda value: (-value[0], value[1]),
    )
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
        return None
    return scored[0][2]


def enrich_catalogue(
    repository: CatalogueRepository,
    provider: AnimeProvider,
    *,
    refresh_interval: int,
    state_dir: Path,
    download_artwork: bool,
    metadata_language: str = "en",
    now: datetime | None = None,
) -> EnrichmentResult:
    """Enrich available titles; isolate provider failures per library entry."""

    timestamp = datetime.now(UTC) if now is None else now
    stale_before = timestamp - timedelta(seconds=refresh_interval)
    enriched = cached = unmatched = disabled = 0
    errors: list[str] = []
    for entry in repository.list_library_entries():
        if not entry.metadata_enabled:
            disabled += 1
            continue
        record = repository.get_provider_record(entry.id, provider.name)
        if record is not None and record.fetched_at >= stale_before:
            cached += 1
            continue
        try:
            provider_id = _select_provider_id(entry, provider, record)
            if provider_id is None:
                unmatched += 1
                continue
            validators = (
                None
                if record is None or record.provider_id != provider_id
                else CacheValidators(record.etag, record.last_modified)
            )
            result = provider.details(provider_id, validators)
            if result.not_modified:
                if record is None:
                    raise ProviderError("provider returned 304 without cached metadata")
                _refresh_cached_record(repository, record, result.validators, timestamp)
                cached += 1
                continue
            details = result.details
            if details is None:
                raise ProviderError("provider returned no anime details")
            episodes = provider.episodes(details.provider_id)
            _store_details(
                repository,
                provider.name,
                entry,
                details,
                episodes,
                timestamp,
                metadata_language,
            )
            if download_artwork and details.artwork_url:
                try:
                    _cache_artwork(
                        repository, provider, entry, details, state_dir, timestamp
                    )
                except (ProviderError, OSError) as error:
                    _record_missing_artwork(
                        repository, provider.name, entry, details.artwork_url
                    )
                    errors.append(f"{entry.relative_path}: artwork: {error}")
            enriched += 1
        except (ProviderError, ValueError, OSError, sqlite3.Error) as error:
            errors.append(f"{entry.relative_path}: {error}")
    return EnrichmentResult(enriched, cached, unmatched, disabled, tuple(errors))


def _select_provider_id(
    entry: LibraryEntry,
    provider: AnimeProvider,
    record: ProviderRecord | None,
) -> str | None:
    if entry.pinned_provider == provider.name and entry.pinned_provider_id:
        return entry.pinned_provider_id
    if record is not None:
        return record.provider_id
    return (
        matched.provider_id
        if (matched := match_candidate(entry.title, provider.search(entry.title)))
        else None
    )


def _store_details(
    repository: CatalogueRepository,
    provider_name: str,
    entry: LibraryEntry,
    details: AnimeDetails,
    episodes: Sequence[ProviderEpisode],
    timestamp: datetime,
    metadata_language: str,
) -> None:
    with repository.transaction():
        record = repository.upsert_provider_record(
            library_entry_id=entry.id,
            provider=provider_name,
            provider_id=details.provider_id,
            canonical_title=_preferred_title(details, metadata_language),
            synopsis=details.synopsis,
            episode_count=details.episode_count,
            raw_data=details.raw_data,
            etag=details.validators.etag,
            last_modified=details.validators.last_modified,
            fetched_at=timestamp,
        )
        repository.replace_aliases(record.id, details.aliases)
        repository.replace_genres(record.id, details.genres)
        repository.replace_relations(record.id, details.relations)
        repository.replace_provider_episodes(record.id, episodes)


def _preferred_title(details: AnimeDetails, language: str) -> str:
    wanted = {
        "en": "english",
        "eng": "english",
        "ja": "japanese",
        "jp": "japanese",
        "jpn": "japanese",
    }.get(language.casefold())
    if wanted is not None:
        for alias_type, title in details.aliases:
            if alias_type == wanted:
                return title
    return details.title


def _refresh_cached_record(
    repository: CatalogueRepository,
    record: ProviderRecord,
    validators: CacheValidators,
    timestamp: datetime,
) -> None:
    repository.upsert_provider_record(
        library_entry_id=record.library_entry_id,
        provider=record.provider,
        provider_id=record.provider_id,
        canonical_title=record.canonical_title,
        synopsis=record.synopsis,
        episode_count=record.episode_count,
        raw_data=json.loads(record.raw_json),
        etag=validators.etag or record.etag,
        last_modified=validators.last_modified or record.last_modified,
        fetched_at=timestamp,
    )


def _cache_artwork(
    repository: CatalogueRepository,
    provider: AnimeProvider,
    entry: LibraryEntry,
    details: AnimeDetails,
    state_dir: Path,
    timestamp: datetime,
) -> None:
    assert details.artwork_url is not None
    extension = _artwork_extension(details.artwork_url)
    relative = f"artwork/{provider.name}-{details.provider_id}{extension}"
    destination = state_dir / relative
    mime_type, size = provider.artwork(details.artwork_url, destination)
    record = repository.get_provider_record(entry.id, provider.name)
    if record is None:
        raise ProviderError("metadata disappeared before artwork was stored")
    repository.upsert_artwork(
        provider_record_id=record.id,
        kind="cover",
        source_url=details.artwork_url,
        relative_path=relative,
        mime_type=mime_type,
        size_bytes=size,
        fetched_at=timestamp,
    )


def _record_missing_artwork(
    repository: CatalogueRepository,
    provider_name: str,
    entry: LibraryEntry,
    source_url: str,
) -> None:
    record = repository.get_provider_record(entry.id, provider_name)
    if record is None:
        return
    repository.upsert_artwork(
        provider_record_id=record.id,
        kind="cover",
        source_url=source_url,
    )


def _urllib_transport(request: HttpRequest) -> HttpResponse:
    http_request = urllib.request.Request(request.url, headers=dict(request.headers))
    try:
        response = urllib.request.urlopen(http_request, timeout=request.timeout)
    except urllib.error.HTTPError as error:
        body = error.read(request.max_bytes + 1)
        return HttpResponse(error.code, dict(error.headers.items()), body)
    with response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > request.max_bytes:
            raise ProviderError("response exceeds configured size limit")
        body = response.read(request.max_bytes + 1)
        if len(body) > request.max_bytes:
            raise ProviderError("response exceeds configured size limit")
        return HttpResponse(int(response.status), dict(response.headers.items()), body)


def _title_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_words, right_words = set(left.split()), set(right.split())
    union = left_words | right_words
    token_score = len(left_words & right_words) / len(union) if union else 0.0
    sequence_score = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return 0.55 * sequence_score + 0.45 * token_score


def _names(candidate: AnimeCandidate) -> tuple[str, ...]:
    return (candidate.title, *candidate.aliases)


def _conditional_headers(
    validators: CacheValidators | None,
) -> dict[str, str]:
    if validators is None:
        return {}
    headers: dict[str, str] = {}
    if validators.etag:
        headers["If-None-Match"] = validators.etag
    if validators.last_modified:
        headers["If-Modified-Since"] = validators.last_modified
    return headers


def _candidate_aliases(record: Mapping[str, object]) -> tuple[str, ...]:
    aliases = [
        value
        for key in ("title_english", "title_japanese")
        if (value := _optional_text(record.get(key))) is not None
    ]
    raw_titles = record.get("titles", [])
    if isinstance(raw_titles, list):
        for item in raw_titles:
            if isinstance(item, dict):
                title = _optional_text(item.get("title"))
                if title:
                    aliases.append(title)
    return tuple(dict.fromkeys(aliases))


def _detail_aliases(
    record: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    aliases: list[tuple[str, str]] = []
    for alias_type, key in (
        ("english", "title_english"),
        ("japanese", "title_japanese"),
    ):
        if (title := _optional_text(record.get(key))) is not None:
            aliases.append((alias_type, title))
    for synonym in _array(record.get("title_synonyms", []), "title_synonyms"):
        if isinstance(synonym, str) and synonym.strip():
            aliases.append(("synonym", synonym.strip()))
    return tuple(dict.fromkeys(aliases))


def _named_values(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ProviderError(f"{name} must be an array")
    return [_text(_mapping(item, name).get("name"), f"{name} name") for item in value]


def _relations(value: object) -> list[Relation]:
    if not isinstance(value, list):
        raise ProviderError("relations must be an array")
    relations: list[Relation] = []
    for group in value:
        record = _mapping(group, "relation")
        relation_type = _text(record.get("relation"), "relation type").casefold()
        for entry in _array(record.get("entry", []), "relation entries"):
            target = _mapping(entry, "relation entry")
            if str(target.get("type", "")).casefold() != "anime":
                continue
            relations.append(
                Relation(
                    relation_type=relation_type,
                    target_provider="jikan",
                    target_provider_id=str(
                        _positive_int(target.get("mal_id"), "relation mal_id")
                    ),
                    target_title=_text(target.get("name"), "relation title"),
                )
            )
    return relations


def _artwork_url(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    jpg = value.get("jpg")
    if not isinstance(jpg, dict):
        return None
    return _optional_text(jpg.get("large_image_url") or jpg.get("image_url"))


def _artwork_extension(url: str) -> str:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.casefold()
    return suffix if suffix in {".gif", ".jpeg", ".jpg", ".png", ".webp"} else ".img"


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProviderError(f"{name} must be an object")
    return value


def _list_field(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise ProviderError(f"{name} must be an array")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ProviderError(f"{name} must be an array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderError(f"{name} must be a positive integer")
    return value


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError(f"{name} must be a non-negative integer or null")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderError("episode air date must be ISO8601 text or null")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderError(f"invalid episode air date: {value}") from error
    if timestamp.tzinfo is None:
        raise ProviderError("episode air date must include a timezone")
    return timestamp.astimezone(UTC)


def _provider_id(value: str) -> str:
    if not value.isdecimal() or int(value) <= 0:
        raise ProviderError("provider ID must be a positive integer")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == wanted),
        None,
    )


def _retry_after(headers: Mapping[str, str]) -> float:
    value = _header(headers, "retry-after")
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        return 0.0
