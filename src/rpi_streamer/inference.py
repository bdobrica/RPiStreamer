"""Bounded, structured OpenAI inference for unresolved local anime names."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

RESPONSES_URL: Final = "https://api.openai.com/v1/responses"
SCHEMA_VERSION: Final = "rpi-streamer-inference-v1"
MAPPING_SCHEMA_VERSION: Final = "rpi-streamer-multi-work-v1"
MAX_FILENAMES: Final = 50
MAX_CANDIDATES: Final = 12
MAX_TITLE_CHARS: Final = 300
MAX_FILENAME_CHARS: Final = 300
MAX_RESPONSE_BYTES: Final = 128 * 1024
MAX_OUTPUT_TOKENS: Final = 2000
MAPPING_KINDS: Final = (
    "episode",
    "movie",
    "ova",
    "oad",
    "ona",
    "special",
    "summary",
    "unknown",
)
_HINT_RE: Final = re.compile(
    r"^(?:E[1-9][0-9]{0,3}|S[1-9][0-9]{0,2}E[1-9][0-9]{0,3}|"
    r"(?:OVA|OAD|ONA|SPECIAL|MOVIE)(?:[ -]?[1-9][0-9]{0,2})?|"
    r"E[1-9][0-9]{0,3}-E[1-9][0-9]{0,3})$",
    re.ASCII,
)


class InferenceError(RuntimeError):
    """A non-fatal inference transport, refusal, or validation failure."""


@dataclass(frozen=True, slots=True)
class EpisodeInference:
    filename: str
    hint: str | None
    confidence: float


@dataclass(frozen=True, slots=True)
class InferenceResult:
    title_hint: str | None
    confidence: float
    reason: str
    episodes: tuple[EpisodeInference, ...]


@dataclass(frozen=True, slots=True)
class MediaMappingInference:
    filename: str
    mal_id: str | None
    kind: str
    episode_start: int | None
    episode_end: int | None
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class MultiWorkInferenceResult:
    mappings: tuple[MediaMappingInference, ...]


Transport = Callable[[urllib.request.Request, float], bytes]


class OpenAIInferenceClient:
    """Small Responses API client with a strict call budget and JSON schema."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.6-luna",
        timeout: float = 30,
        max_calls: int = 3,
        transport: Transport | None = None,
    ) -> None:
        if not api_key or timeout <= 0 or max_calls <= 0:
            raise ValueError("invalid OpenAI inference configuration")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = 0
        self._transport = transport or _transport

    def infer(self, title: str, filenames: Sequence[str]) -> InferenceResult:
        """Infer one title hint and conservative hints for submitted basenames."""

        if self.calls >= self.max_calls:
            raise InferenceError("OpenAI per-scan call limit reached")
        clean_title = _bounded_text(title, MAX_TITLE_CHARS, "title")
        clean_filenames = tuple(
            _bounded_text(name, MAX_FILENAME_CHARS, "filename")
            for name in filenames[:MAX_FILENAMES]
        )
        if len(filenames) > MAX_FILENAMES:
            raise InferenceError(f"too many filenames (maximum {MAX_FILENAMES})")
        self.calls += 1
        body = json.dumps(
            {
                "model": self.model,
                "store": False,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "instructions": (
                    "Normalize a local anime folder name for metadata search and "
                    "infer episode labels only when strongly supported by each "
                    "basename. Do not invent a MyAnimeList ID. Return null when "
                    "uncertain. Episode hints must use E1, S1E1, E1-E2, OVA1, "
                    "OAD1, ONA1, SPECIAL1, or MOVIE1."
                ),
                "input": json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "directory_name": clean_title,
                        "filenames": clean_filenames,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "rpi_streamer_inference",
                        "strict": True,
                        "schema": _schema(),
                    }
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            RESPONSES_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "RPi-Streamer/0.1",
            },
            method="POST",
        )
        try:
            raw = self._transport(request, self.timeout)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise InferenceError(f"OpenAI request failed: {error}") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InferenceError("OpenAI response exceeds size limit")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InferenceError("OpenAI returned malformed JSON") from error
        text = _output_text(response)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise InferenceError("OpenAI structured output is malformed") from error
        return _validate(payload, clean_filenames)

    def infer_multi_work(
        self,
        collection_name: str,
        directory_name: str,
        files: Sequence[Mapping[str, object]],
        candidates: Sequence[Mapping[str, object]],
    ) -> MultiWorkInferenceResult:
        """Map unresolved basenames only among supplied verified candidates."""

        if self.calls >= self.max_calls:
            raise InferenceError("OpenAI per-scan call limit reached")
        if not files or len(files) > MAX_FILENAMES:
            raise InferenceError(
                f"invalid mapping filename count (maximum {MAX_FILENAMES})"
            )
        if not candidates or len(candidates) > MAX_CANDIDATES:
            raise InferenceError(
                f"invalid mapping candidate count (maximum {MAX_CANDIDATES})"
            )
        clean_collection = _bounded_text(
            collection_name, MAX_TITLE_CHARS, "collection name"
        )
        clean_directory = _bounded_text(
            directory_name, MAX_TITLE_CHARS, "directory name"
        )
        clean_files = _mapping_files(files)
        clean_candidates = _mapping_candidates(candidates)
        candidate_ids = tuple(str(item["mal_id"]) for item in clean_candidates)
        self.calls += 1
        body = json.dumps(
            {
                "model": self.model,
                "store": False,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "instructions": (
                    "Map every submitted anime media basename to one supplied "
                    "verified candidate or null. Prefer null over guessing. "
                    "Use only the filename facts and candidate summaries. Never "
                    "invent or alter a MAL ID, filename, or manual decision."
                ),
                "input": json.dumps(
                    {
                        "schema_version": MAPPING_SCHEMA_VERSION,
                        "collection_name": clean_collection,
                        "directory_name": clean_directory,
                        "files": clean_files,
                        "candidates": clean_candidates,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "rpi_streamer_multi_work_mapping",
                        "strict": True,
                        "schema": _mapping_schema(candidate_ids),
                    }
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            RESPONSES_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "RPi-Streamer/0.1",
            },
            method="POST",
        )
        try:
            raw = self._transport(request, self.timeout)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise InferenceError(f"OpenAI request failed: {error}") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InferenceError("OpenAI response exceeds size limit")
        try:
            response = json.loads(raw.decode("utf-8"))
            payload = json.loads(_output_text(response))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InferenceError("OpenAI structured output is malformed") from error
        return _validate_mapping(
            payload,
            tuple(cast(str, item["filename"]) for item in clean_files),
            candidate_ids,
        )


def result_data(result: InferenceResult) -> Mapping[str, object]:
    """Return the stable, non-secret cache representation."""

    return {
        "schema_version": SCHEMA_VERSION,
        "title_hint": result.title_hint,
        "confidence": result.confidence,
        "reason": result.reason,
        "episodes": [
            {
                "filename": episode.filename,
                "hint": episode.hint,
                "confidence": episode.confidence,
            }
            for episode in result.episodes
        ],
    }


def result_from_data(data: object, filenames: Sequence[str]) -> InferenceResult:
    """Validate a cached result against the current submitted basenames."""

    return _validate(data, filenames)


def mapping_result_data(result: MultiWorkInferenceResult) -> Mapping[str, object]:
    """Return the stable v2 cache representation."""

    return {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "mappings": [
            {
                "filename": item.filename,
                "mal_id": item.mal_id,
                "kind": item.kind,
                "episode_start": item.episode_start,
                "episode_end": item.episode_end,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in result.mappings
        ],
    }


def mapping_result_from_data(
    data: object, filenames: Sequence[str], candidate_ids: Sequence[str]
) -> MultiWorkInferenceResult:
    """Validate a cached v2 result against the exact current request."""

    return _validate_mapping(data, filenames, candidate_ids)


def _schema() -> Mapping[str, object]:
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "enum": [SCHEMA_VERSION]},
            "title_hint": nullable_string,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 160},
            "episodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "filename": {"type": "string"},
                        "hint": nullable_string,
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["filename", "hint", "confidence"],
                },
            },
        },
        "required": [
            "schema_version",
            "title_hint",
            "confidence",
            "reason",
            "episodes",
        ],
    }


def _mapping_schema(candidate_ids: Sequence[str]) -> Mapping[str, object]:
    nullable_integer = {"type": ["integer", "null"], "minimum": 1, "maximum": 9999}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": [MAPPING_SCHEMA_VERSION],
            },
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "filename": {"type": "string"},
                        "mal_id": {
                            "anyOf": [
                                {"type": "string", "enum": list(candidate_ids)},
                                {"type": "null"},
                            ]
                        },
                        "kind": {"type": "string", "enum": list(MAPPING_KINDS)},
                        "episode_start": nullable_integer,
                        "episode_end": nullable_integer,
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "reason": {"type": "string", "maxLength": 120},
                    },
                    "required": [
                        "filename",
                        "mal_id",
                        "kind",
                        "episode_start",
                        "episode_end",
                        "confidence",
                        "reason",
                    ],
                },
            },
        },
        "required": ["schema_version", "mappings"],
    }


def _output_text(response: object) -> str:
    if not isinstance(response, dict):
        raise InferenceError("OpenAI response must be an object")
    if response.get("status") != "completed":
        raise InferenceError("OpenAI response was not completed")
    texts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        raise InferenceError("OpenAI response has no output")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise InferenceError("OpenAI refused the inference request")
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    texts.append(value)
    if len(texts) != 1:
        raise InferenceError("OpenAI response has unexpected text output")
    return texts[0]


def _validate(payload: object, filenames: Sequence[str]) -> InferenceResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "title_hint",
        "confidence",
        "reason",
        "episodes",
    }:
        raise InferenceError("OpenAI output has unexpected fields")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise InferenceError("OpenAI output schema version mismatch")
    confidence = _confidence(payload["confidence"])
    title_hint = payload["title_hint"]
    if title_hint is not None:
        title_hint = _bounded_text(title_hint, MAX_TITLE_CHARS, "title hint").strip()
        if not title_hint:
            raise InferenceError("OpenAI title hint is empty")
    reason = _bounded_text(payload["reason"], 160, "reason")
    episodes_value = payload["episodes"]
    if not isinstance(episodes_value, list):
        raise InferenceError("OpenAI episodes must be a list")
    submitted = set(filenames)
    seen: set[str] = set()
    episodes: list[EpisodeInference] = []
    for value in episodes_value:
        if not isinstance(value, dict) or set(value) != {
            "filename",
            "hint",
            "confidence",
        }:
            raise InferenceError("OpenAI episode output has unexpected fields")
        filename = value["filename"]
        if (
            not isinstance(filename, str)
            or filename not in submitted
            or filename in seen
        ):
            raise InferenceError("OpenAI episode filename was not uniquely submitted")
        seen.add(filename)
        hint = value["hint"]
        if hint is not None and (
            not isinstance(hint, str) or _HINT_RE.fullmatch(hint.upper()) is None
        ):
            raise InferenceError("OpenAI returned an invalid episode hint")
        episodes.append(
            EpisodeInference(
                filename,
                None if hint is None else hint.upper(),
                _confidence(value["confidence"]),
            )
        )
    return InferenceResult(title_hint, confidence, reason, tuple(episodes))


def _validate_mapping(
    payload: object,
    filenames: Sequence[str],
    candidate_ids: Sequence[str],
) -> MultiWorkInferenceResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "mappings",
    }:
        raise InferenceError("OpenAI mapping output has unexpected fields")
    if payload["schema_version"] != MAPPING_SCHEMA_VERSION:
        raise InferenceError("OpenAI mapping schema version mismatch")
    values = payload["mappings"]
    if not isinstance(values, list) or len(values) != len(filenames):
        raise InferenceError("OpenAI mapping output is incomplete")
    submitted = set(filenames)
    allowed_ids = set(candidate_ids)
    seen: set[str] = set()
    mappings: list[MediaMappingInference] = []
    expected_fields = {
        "filename",
        "mal_id",
        "kind",
        "episode_start",
        "episode_end",
        "confidence",
        "reason",
    }
    for value in values:
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise InferenceError("OpenAI mapping entry has unexpected fields")
        filename = value["filename"]
        if (
            not isinstance(filename, str)
            or filename not in submitted
            or filename in seen
        ):
            raise InferenceError("OpenAI mapping filename was not uniquely submitted")
        seen.add(filename)
        mal_id = value["mal_id"]
        if mal_id is not None and (
            not isinstance(mal_id, str) or mal_id not in allowed_ids
        ):
            raise InferenceError("OpenAI mapping returned an unverified MAL ID")
        kind = value["kind"]
        if not isinstance(kind, str) or kind not in MAPPING_KINDS:
            raise InferenceError("OpenAI mapping returned an invalid kind")
        episode_start = _nullable_episode(value["episode_start"])
        episode_end = _nullable_episode(value["episode_end"])
        if (episode_start is None) != (episode_end is None):
            raise InferenceError("OpenAI mapping episode range is incomplete")
        if (
            episode_start is not None
            and episode_end is not None
            and episode_end < episode_start
        ):
            raise InferenceError("OpenAI mapping episode range is reversed")
        if kind == "episode" and mal_id is not None and episode_start is None:
            raise InferenceError("OpenAI episode mapping has no episode number")
        reason = _bounded_text(value["reason"], 120, "mapping reason")
        mappings.append(
            MediaMappingInference(
                filename,
                mal_id,
                kind,
                episode_start,
                episode_end,
                _confidence(value["confidence"]),
                reason,
            )
        )
    if seen != submitted:
        raise InferenceError("OpenAI mapping output is incomplete")
    return MultiWorkInferenceResult(tuple(mappings))


def _mapping_files(
    files: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    allowed = {
        "filename",
        "season",
        "episode_start",
        "episode_end",
        "special_kind",
        "explicit_ordinal",
        "markers",
    }
    cleaned: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in files:
        if set(value) != allowed:
            raise InferenceError("mapping file facts have unexpected fields")
        filename = _bounded_text(value["filename"], MAX_FILENAME_CHARS, "filename")
        if filename in seen:
            raise InferenceError("mapping filenames must be unique")
        seen.add(filename)
        season = _optional_bounded_integer(value["season"], 999, "season")
        episode_start = _nullable_episode(value["episode_start"])
        episode_end = _nullable_episode(value["episode_end"])
        if (episode_start is None) != (episode_end is None) or (
            episode_start is not None
            and episode_end is not None
            and episode_end < episode_start
        ):
            raise InferenceError("mapping file episode range is invalid")
        special_kind = value["special_kind"]
        if special_kind is not None and special_kind not in MAPPING_KINDS:
            raise InferenceError("mapping file special kind is invalid")
        ordinal = _optional_bounded_integer(
            value["explicit_ordinal"], 999, "explicit ordinal"
        )
        markers = value["markers"]
        if (
            not isinstance(markers, list | tuple)
            or len(markers) > 32
            or any(
                not isinstance(marker, str) or not marker or len(marker) > 64
                for marker in markers
            )
        ):
            raise InferenceError("mapping file markers are invalid")
        cleaned.append(
            {
                "filename": filename,
                "season": season,
                "episode_start": episode_start,
                "episode_end": episode_end,
                "special_kind": special_kind,
                "explicit_ordinal": ordinal,
                "markers": list(markers),
            }
        )
    return tuple(cleaned)


def _mapping_candidates(
    candidates: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    allowed = {
        "mal_id",
        "title",
        "media_type",
        "episode_count",
        "relation_type",
        "relation_distance",
        "order",
    }
    cleaned: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in candidates:
        if set(value) != allowed:
            raise InferenceError("mapping candidate has unexpected fields")
        mal_id = _bounded_text(value["mal_id"], 32, "MAL ID")
        if mal_id in seen or not mal_id.isdigit():
            raise InferenceError("mapping candidate MAL IDs must be unique")
        seen.add(mal_id)
        title = _bounded_text(value["title"], MAX_TITLE_CHARS, "candidate title")
        media_type = value["media_type"]
        if media_type is not None:
            media_type = _bounded_text(media_type, 40, "candidate media type")
        episode_count = _optional_bounded_integer(
            value["episode_count"], 9999, "candidate episode count"
        )
        relation_type = _bounded_text(
            value["relation_type"], 80, "candidate relation type"
        )
        relation_distance = _optional_bounded_integer(
            value["relation_distance"], 3, "candidate relation distance"
        )
        order = _optional_bounded_integer(value["order"], 10000, "candidate order")
        if order is None:
            raise InferenceError("candidate order is required")
        cleaned.append(
            {
                "mal_id": mal_id,
                "title": title,
                "media_type": media_type,
                "episode_count": episode_count,
                "relation_type": relation_type,
                "relation_distance": relation_distance,
                "order": order,
            }
        )
    return tuple(cleaned)


def _nullable_episode(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999:
        raise InferenceError("OpenAI mapping episode is outside 1..9999")
    return value


def _optional_bounded_integer(value: object, maximum: int, name: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise InferenceError(f"{name} is invalid")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InferenceError("OpenAI confidence must be numeric")
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise InferenceError("OpenAI confidence is outside 0..1")
    return parsed


def _bounded_text(value: object, maximum: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise InferenceError(f"{name} is invalid or too long")
    return value


def _transport(request: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise InferenceError(f"OpenAI returned HTTP {response.status}")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_RESPONSE_BYTES:
                raise InferenceError("OpenAI response exceeds size limit")
            return cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as error:
        raise InferenceError(f"OpenAI returned HTTP {error.code}") from error
