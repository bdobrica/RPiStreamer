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
MAX_FILENAMES: Final = 50
MAX_TITLE_CHARS: Final = 300
MAX_FILENAME_CHARS: Final = 300
MAX_RESPONSE_BYTES: Final = 128 * 1024
MAX_OUTPUT_TOKENS: Final = 2000
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
