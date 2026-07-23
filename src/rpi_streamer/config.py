"""Configuration loading and validation for RPi Streamer."""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

SECTION: Final = "rpi-streamer"
DEFAULT_CONFIG_PATH: Final = Path("/etc/rpi-streamer/rpi-streamer.ini")
ENV_PREFIX: Final = "RPI_STREAMER_"

_DURATION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)([smhd]?)$", re.ASCII)
_LANGUAGE_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$", re.ASCII)
_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})
_PROVIDERS: Final = frozenset({"jikan", "none"})
_LOG_LEVELS: Final = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class ConfigurationError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, normalized application settings."""

    media_root: Path = Path("/mnt/anime")
    state_dir: Path = Path("/var/lib/rpi-streamer")
    site_dir: Path = Path("/var/lib/rpi-streamer/site")
    database_path: Path = Path("/var/lib/rpi-streamer/catalogue.db")
    scan_interval: int = 3600
    metadata_provider: str = "jikan"
    metadata_refresh_interval: int = 30 * 86400
    metadata_language: str = "en"
    download_artwork: bool = True
    log_level: str = "INFO"

    def as_serializable(self) -> dict[str, str | int | bool]:
        """Return deterministic, non-secret configuration output."""

        values = asdict(self)
        return {
            "database_path": str(values["database_path"]),
            "download_artwork": bool(values["download_artwork"]),
            "log_level": str(values["log_level"]),
            "media_root": str(values["media_root"]),
            "metadata_language": str(values["metadata_language"]),
            "metadata_provider": str(values["metadata_provider"]),
            "metadata_refresh_interval": int(values["metadata_refresh_interval"]),
            "scan_interval": int(values["scan_interval"]),
            "site_dir": str(values["site_dir"]),
            "state_dir": str(values["state_dir"]),
        }

    def to_json(self) -> str:
        """Render normalized settings for validation and diagnostics."""

        return json.dumps(self.as_serializable(), indent=2, sort_keys=True)


_DEFAULT_TEXT: Final[dict[str, str]] = {
    "media_root": "/mnt/anime",
    "state_dir": "/var/lib/rpi-streamer",
    "site_dir": "/var/lib/rpi-streamer/site",
    "database_path": "/var/lib/rpi-streamer/catalogue.db",
    "scan_interval": "1h",
    "metadata_provider": "jikan",
    "metadata_refresh_interval": "30d",
    "metadata_language": "en",
    "download_artwork": "true",
    "log_level": "INFO",
}
_ENV_BY_KEY: Final = {key: f"{ENV_PREFIX}{key.upper()}" for key in _DEFAULT_TEXT}


def parse_duration(value: str, *, name: str) -> int:
    """Parse a non-negative duration with an optional s/m/h/d suffix."""

    match = _DURATION_RE.fullmatch(value.strip().lower())
    if match is None:
        raise ConfigurationError(
            f"{name} must be a non-negative integer followed by s, m, h, or d"
        )
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount * multiplier


def parse_bool(value: str, *, name: str) -> bool:
    """Parse a conventional, case-insensitive boolean value."""

    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    accepted = ", ".join(sorted(_TRUE_VALUES | _FALSE_VALUES))
    raise ConfigurationError(f"{name} must be one of: {accepted}")


def resolve_config_path(
    cli_path: str | os.PathLike[str] | None,
    environ: Mapping[str, str],
) -> tuple[Path, bool]:
    """Resolve config path and whether its presence was explicitly requested."""

    if cli_path is not None:
        return Path(cli_path).expanduser(), True
    env_path = environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser(), True
    return DEFAULT_CONFIG_PATH, False


def load_settings(
    *,
    config_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings with environment-over-INI-over-default precedence."""

    environment = os.environ if environ is None else environ
    selected_path, explicit = resolve_config_path(config_path, environment)
    values = dict(_DEFAULT_TEXT)

    if selected_path.exists():
        values.update(_read_ini(selected_path))
    elif explicit:
        raise ConfigurationError(f"configuration file not found: {selected_path}")

    for key, variable in _ENV_BY_KEY.items():
        if variable in environment:
            values[key] = environment[variable]

    settings = Settings(
        media_root=_absolute_path(values["media_root"], "media_root"),
        state_dir=_absolute_path(values["state_dir"], "state_dir"),
        site_dir=_absolute_path(values["site_dir"], "site_dir"),
        database_path=_absolute_path(values["database_path"], "database_path"),
        scan_interval=parse_duration(values["scan_interval"], name="scan_interval"),
        metadata_provider=values["metadata_provider"].strip().lower(),
        metadata_refresh_interval=parse_duration(
            values["metadata_refresh_interval"],
            name="metadata_refresh_interval",
        ),
        metadata_language=values["metadata_language"].strip(),
        download_artwork=parse_bool(
            values["download_artwork"], name="download_artwork"
        ),
        log_level=values["log_level"].strip().upper(),
    )
    _validate(settings)
    return settings


def _read_ini(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (OSError, UnicodeError, configparser.Error) as error:
        raise ConfigurationError(
            f"cannot read configuration file {path}: {error}"
        ) from error

    sections = parser.sections()
    if SECTION not in sections:
        raise ConfigurationError(f"configuration file {path} must contain [{SECTION}]")
    unexpected_sections = sorted(set(sections) - {SECTION})
    if unexpected_sections:
        joined = ", ".join(unexpected_sections)
        raise ConfigurationError(f"unexpected configuration section(s): {joined}")

    raw = dict(parser[SECTION])
    unknown = sorted(set(raw) - set(_DEFAULT_TEXT))
    if unknown:
        raise ConfigurationError(
            f"unknown configuration option(s): {', '.join(unknown)}"
        )
    return raw


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path")
    return Path(os.path.abspath(path))


def _validate(settings: Settings) -> None:
    if not settings.media_root.is_dir():
        raise ConfigurationError(
            f"media_root is not a directory: {settings.media_root}"
        )
    if not os.access(settings.media_root, os.R_OK | os.X_OK):
        raise ConfigurationError(f"media_root is not readable: {settings.media_root}")

    if settings.metadata_provider not in _PROVIDERS:
        raise ConfigurationError(
            f"metadata_provider must be one of: {', '.join(sorted(_PROVIDERS))}"
        )
    if not _LANGUAGE_RE.fullmatch(settings.metadata_language):
        raise ConfigurationError(
            "metadata_language must contain 1-16 letters, digits, '_' or '-'"
        )
    if settings.log_level not in _LOG_LEVELS:
        raise ConfigurationError(
            f"log_level must be one of: {', '.join(sorted(_LOG_LEVELS))}"
        )
    if settings.metadata_refresh_interval <= 0:
        raise ConfigurationError("metadata_refresh_interval must be greater than zero")

    paths = {
        "media_root": settings.media_root,
        "state_dir": settings.state_dir,
        "site_dir": settings.site_dir,
        "database_path": settings.database_path,
    }
    for first_name, first_path in paths.items():
        for second_name, second_path in paths.items():
            if first_name < second_name and first_path == second_path:
                raise ConfigurationError(
                    f"{first_name} and {second_name} must be distinct"
                )

    if _is_within(settings.state_dir, settings.media_root):
        raise ConfigurationError("state_dir must not be inside media_root")
    if _is_within(settings.site_dir, settings.media_root):
        raise ConfigurationError("site_dir must not be inside media_root")
    if _is_within(settings.database_path, settings.media_root):
        raise ConfigurationError("database_path must not be inside media_root")
    if settings.database_path.exists() and settings.database_path.is_dir():
        raise ConfigurationError(
            f"database_path must not be a directory: {settings.database_path}"
        )

    for name, path in (
        ("state_dir", settings.state_dir),
        ("site_dir", settings.site_dir),
        ("database_path parent", settings.database_path.parent),
    ):
        _validate_writable_target(name, path)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_writable_target(name: str, path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ConfigurationError(f"{name} is not a directory: {path}")
        if not os.access(path, os.W_OK | os.X_OK):
            raise ConfigurationError(f"{name} is not writable: {path}")
        return

    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise ConfigurationError(
            f"{name} cannot be created below writable directory: {ancestor}"
        )


def configure_logging(level: str) -> None:
    """Configure concise service-friendly logging."""

    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
