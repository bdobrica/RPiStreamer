"""Render a safe Nginx site from the resolved application configuration."""

from __future__ import annotations

import os
import re
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

from rpi_streamer.config import Settings

_LISTEN_RE: Final = re.compile(
    r"^(?:127\.0\.0\.1|0\.0\.0\.0|\[[0-9A-Fa-f:]+\]|[0-9A-Za-z.-]+):[1-9][0-9]{0,4}$"
)


def render_nginx(settings: Settings, listen: str) -> str:
    """Return an Nginx server block using the resolved media and site paths."""

    if not _LISTEN_RE.fullmatch(listen):
        raise ValueError("listen must be a host:port value without whitespace")
    port = int(listen.rsplit(":", 1)[1])
    if port > 65535:
        raise ValueError("listen port must be between 1 and 65535")
    template = (
        resources.files("rpi_streamer")
        .joinpath("nginx", "rpi-streamer.conf.template")
        .read_text(encoding="utf-8")
    )
    media = _nginx_path(settings.media_root, trailing=True)
    site = _nginx_path(settings.site_dir, trailing=True)
    return (
        template.replace("__LISTEN__", listen)
        .replace("__SITE_ROOT__", site)
        .replace("__MEDIA_ROOT__", media)
    )


def write_nginx(path: Path, content: str) -> None:
    """Atomically replace an Nginx candidate file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _nginx_path(path: Path, *, trailing: bool) -> str:
    value = str(path)
    if any(ord(char) < 32 or char in {'"', "\\", "$", "{", "}", ";"} for char in value):
        raise ValueError(f"path contains characters unsafe for Nginx: {path}")
    return value.rstrip("/") + ("/" if trailing else "")
