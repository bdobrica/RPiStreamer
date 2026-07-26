"""Strict, bounded parsing for per-collection sidecar configuration."""

from __future__ import annotations

import configparser
import fnmatch
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from rpi_streamer.config import parse_bool

ROOT_SECTION: Final = "rpi-streamer"
ROOT_KEYS: Final = frozenset(
    {
        "display_title",
        "sort_title",
        "metadata_enabled",
        "mal_id",
        "related_mal_ids",
    }
)
WORK_KEYS: Final = frozenset(
    {
        "mal_id",
        "label",
        "files",
        "season",
        "local_episode_range",
        "episode_offset",
        "kind",
        "order",
    }
)
MEDIA_KEYS: Final = frozenset(
    {"file", "mal_id", "episode", "episode_end", "kind", "label"}
)
KINDS: Final = frozenset(
    {"episode", "movie", "ova", "oad", "ona", "special", "summary", "unknown"}
)
MAX_WORKS: Final = 12
MAX_MEDIA: Final = 50
MAX_SECTIONS: Final = 64
MAX_CANDIDATES: Final = 12
MAX_PATTERNS_PER_WORK: Final = 8
MAX_PATTERN_LENGTH: Final = 256
MAX_PATTERN_CHARACTERS: Final = 2048
MAX_LABEL_LENGTH: Final = 120
MAX_BASENAME_LENGTH: Final = 300
MAX_EPISODE: Final = 9999
MAX_ORDER: Final = 10000

_SECTION_RE: Final = re.compile(r'^(work|media) "([^"]+)"$')
_NAME_RE: Final = re.compile(r'^[^"\x00-\x1f\x7f]{1,64}$')
EMPTY_RULES_DIGEST: Final = hashlib.sha256(
    b'{"mal_id":null,"media":[],"related_mal_ids":[],"version":1,"works":[]}'
).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkRule:
    name: str
    mal_id: str
    label: str | None
    files: tuple[str, ...]
    season: int | None
    local_episode_start: int | None
    local_episode_end: int | None
    episode_offset: int
    kind: str | None
    order: int

    def matches(
        self, basename: str, *, season: int | None, episode_start: int | None
    ) -> bool:
        if self.files and not any(
            fnmatch.fnmatchcase(basename.casefold(), pattern.casefold())
            for pattern in self.files
        ):
            return False
        if self.season is not None and season != self.season:
            return False
        if self.local_episode_start is not None and (
            episode_start is None
            or not self.local_episode_start
            <= episode_start
            <= (self.local_episode_end or self.local_episode_start)
        ):
            return False
        return bool(
            self.files
            or self.season is not None
            or self.local_episode_start is not None
        )


@dataclass(frozen=True, slots=True)
class MediaOverride:
    name: str
    file: str
    mal_id: str
    episode: int | None
    episode_end: int | None
    kind: str | None
    label: str | None


@dataclass(frozen=True, slots=True)
class Sidecar:
    display_title: str | None = None
    sort_title: str | None = None
    metadata_enabled: bool = True
    mal_id: str | None = None
    related_mal_ids: tuple[str, ...] = ()
    works: tuple[WorkRule, ...] = ()
    media: tuple[MediaOverride, ...] = ()
    digest: str = EMPTY_RULES_DIGEST

    @property
    def manual_candidate_ids(self) -> tuple[str, ...]:
        values = [
            *(item for item in (self.mal_id,) if item is not None),
            *self.related_mal_ids,
            *(rule.mal_id for rule in self.works),
            *(override.mal_id for override in self.media),
        ]
        return tuple(dict.fromkeys(values))


def read_sidecar(path: Path) -> Sidecar:
    """Parse one UTF-8 sidecar without interpolation."""

    if not path.exists():
        return Sidecar()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    with path.open(encoding="utf-8") as stream:
        parser.read_file(stream)
    sections = parser.sections()
    if not sections or sections[0] != ROOT_SECTION:
        raise ValueError(f"first section must be [{ROOT_SECTION}]")
    if len(sections) > MAX_SECTIONS:
        raise ValueError(f"sidecar has more than {MAX_SECTIONS} sections")

    root = _values(parser, ROOT_SECTION, ROOT_KEYS)
    display_title = _label(root.get("display_title"))
    sort_title = _label(root.get("sort_title"))
    metadata_enabled = parse_bool(
        root.get("metadata_enabled", "true"), name="metadata_enabled"
    )
    mal_id = _optional_id(root.get("mal_id"), "mal_id")
    related = _id_list(root.get("related_mal_ids", ""))

    works: list[WorkRule] = []
    media: list[MediaOverride] = []
    names: set[str] = set()
    exact_files: set[str] = set()
    pattern_characters = 0
    for section in sections[1:]:
        match = _SECTION_RE.fullmatch(section)
        if match is None or not _NAME_RE.fullmatch(match.group(2)):
            raise ValueError(f"invalid section [{section}]")
        section_type, name = match.groups()
        if name in names:
            raise ValueError(f"duplicate section name {name!r}")
        names.add(name)
        if section_type == "work":
            rule = _work_rule(name, _values(parser, section, WORK_KEYS))
            pattern_characters += sum(len(pattern) for pattern in rule.files)
            works.append(rule)
        else:
            override = _media_override(name, _values(parser, section, MEDIA_KEYS))
            if override.file in exact_files:
                raise ValueError(f"duplicate exact file {override.file!r}")
            exact_files.add(override.file)
            media.append(override)
    if len(works) > MAX_WORKS:
        raise ValueError(f"sidecar has more than {MAX_WORKS} work sections")
    if len(media) > MAX_MEDIA:
        raise ValueError(f"sidecar has more than {MAX_MEDIA} media sections")
    if pattern_characters > MAX_PATTERN_CHARACTERS:
        raise ValueError("sidecar glob patterns are too large")

    sidecar = Sidecar(
        display_title,
        sort_title,
        metadata_enabled,
        mal_id,
        related,
        tuple(works),
        tuple(media),
    )
    if len(sidecar.manual_candidate_ids) > MAX_CANDIDATES:
        raise ValueError(f"sidecar has more than {MAX_CANDIDATES} candidate MAL IDs")
    selectorless = [
        rule
        for rule in works
        if not rule.files
        and rule.season is None
        and rule.local_episode_start is None
        and rule.mal_id != mal_id
    ]
    if selectorless:
        raise ValueError("only the primary work may omit selectors")
    payload = {
        "version": 1,
        "mal_id": mal_id,
        "related_mal_ids": related,
        "works": [asdict(rule) for rule in works],
        "media": [asdict(override) for override in media],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    return Sidecar(
        display_title,
        sort_title,
        metadata_enabled,
        mal_id,
        related,
        tuple(works),
        tuple(media),
        digest,
    )


def validate_local_files(sidecar: Sidecar, basenames: set[str]) -> None:
    """Reject exact references to missing files and unsafe glob outcomes."""

    for override in sidecar.media:
        if override.file not in basenames:
            raise ValueError(f"exact file {override.file!r} does not exist")
    for rule in sidecar.works:
        for pattern in rule.files:
            if not any(
                fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
                for name in basenames
            ):
                raise ValueError(
                    f"work {rule.name!r} glob {pattern!r} matches no local file"
                )


def _values(
    parser: configparser.ConfigParser, section: str, allowed: frozenset[str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in parser.items(section, raw=True):
        normalized = key.casefold()
        if normalized not in allowed:
            raise ValueError(f"unknown option {key!r} in [{section}]")
        if normalized in values:
            raise ValueError(f"duplicate option {key!r} in [{section}]")
        values[normalized] = value
    return values


def _work_rule(name: str, values: dict[str, str]) -> WorkRule:
    mal_id = _required_id(values.get("mal_id"), f"work {name!r} mal_id")
    label = _label(values.get("label"))
    patterns = tuple(
        line.strip() for line in values.get("files", "").splitlines() if line.strip()
    )
    if len(patterns) > MAX_PATTERNS_PER_WORK:
        raise ValueError(f"work {name!r} has too many file globs")
    for pattern in patterns:
        _validate_pattern(pattern)
    season = _bounded_positive(values.get("season"), "season")
    range_start, range_end = _range(
        values.get("local_episode_range"), "local_episode_range"
    )
    offset = _signed(values.get("episode_offset", "0"), "episode_offset")
    kind = _kind(values.get("kind"))
    order = _bounded_nonnegative(values.get("order", "0"), "order")
    return WorkRule(
        name,
        mal_id,
        label,
        patterns,
        season,
        range_start,
        range_end,
        offset,
        kind,
        order,
    )


def _media_override(name: str, values: dict[str, str]) -> MediaOverride:
    basename = _required_text(values.get("file"), f"media {name!r} file")
    if len(basename) > MAX_BASENAME_LENGTH:
        raise ValueError("exact filename is too long")
    _validate_basename(basename)
    episode = _bounded_positive(values.get("episode"), "episode")
    episode_end = _bounded_positive(values.get("episode_end"), "episode_end")
    if episode_end is not None and episode is None:
        raise ValueError("episode_end requires episode")
    if episode is not None and episode_end is not None and episode_end < episode:
        raise ValueError("episode_end cannot be less than episode")
    return MediaOverride(
        name,
        basename,
        _required_id(values.get("mal_id"), f"media {name!r} mal_id"),
        episode,
        episode if episode is not None and episode_end is None else episode_end,
        _kind(values.get("kind")),
        _label(values.get("label")),
    )


def _validate_pattern(pattern: str) -> None:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError("file glob is too long")
    _validate_basename(pattern)
    try:
        re.compile(fnmatch.translate(pattern))
    except re.error as error:
        raise ValueError(f"invalid file glob {pattern!r}") from error


def _validate_basename(value: str) -> None:
    if (
        "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or Path(value).is_absolute()
    ):
        raise ValueError("file values and globs must be basenames")


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _required_text(value: str | None, name: str) -> str:
    cleaned = "" if value is None else value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    return cleaned


def _label(value: str | None) -> str | None:
    label = _optional_text(value, "label")
    if label is not None and len(label) > MAX_LABEL_LENGTH:
        raise ValueError("label is too long")
    return label


def _optional_id(value: str | None, name: str) -> str | None:
    return None if value is None or not value.strip() else _required_id(value, name)


def _required_id(value: str | None, name: str) -> str:
    cleaned = _required_text(value, name)
    if not cleaned.isdecimal() or int(cleaned) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return str(int(cleaned))


def _id_list(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    parts = value.split(",")
    if any(not item.strip() for item in parts):
        raise ValueError("related_mal_ids contains an empty value")
    return tuple(dict.fromkeys(_required_id(item, "related_mal_ids") for item in parts))


def _bounded_positive(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    cleaned = _required_text(value, name)
    if not cleaned.isdecimal() or not 1 <= int(cleaned) <= MAX_EPISODE:
        raise ValueError(f"{name} must be between 1 and {MAX_EPISODE}")
    return int(cleaned)


def _bounded_nonnegative(value: str, name: str) -> int:
    cleaned = _required_text(value, name)
    if not cleaned.isdecimal() or not 0 <= int(cleaned) <= MAX_ORDER:
        raise ValueError(f"{name} must be between 0 and {MAX_ORDER}")
    return int(cleaned)


def _signed(value: str, name: str) -> int:
    cleaned = _required_text(value, name)
    try:
        parsed = int(cleaned)
    except ValueError as error:
        raise ValueError(f"{name} must be a signed integer") from error
    if abs(parsed) > MAX_EPISODE:
        raise ValueError(f"{name} exceeds {MAX_EPISODE}")
    return parsed


def _range(value: str | None, name: str) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    if match is None:
        raise ValueError(f"{name} must use START-END")
    start, end = (int(part) for part in match.groups())
    if not 1 <= start <= end <= MAX_EPISODE:
        raise ValueError(f"{name} must be within 1-{MAX_EPISODE}")
    return start, end


def _kind(value: str | None) -> str | None:
    if value is None:
        return None
    kind = _required_text(value, "kind").casefold()
    if kind not in KINDS:
        raise ValueError(f"unsupported kind {kind!r}")
    return kind
