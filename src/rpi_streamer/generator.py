"""Deterministic, escaped static catalogue generation and atomic publishing."""

from __future__ import annotations

import hashlib
import html
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from typing import Final
from urllib.parse import quote

from rpi_streamer.database import (
    Artwork,
    CatalogueRepository,
    LibraryEntry,
    ProviderRecord,
    ScanRun,
)
from rpi_streamer.metadata import MAX_ARTWORK_BYTES

_IMAGE_TYPES: Final = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_TEMPLATES: Final = ("base", "index", "title", "genres", "genre")


class GenerationError(RuntimeError):
    """Raised when a catalogue cannot be rendered or safely published."""


@dataclass(frozen=True, slots=True)
class GeneratedSite:
    title_count: int
    page_count: int
    site_dir: Path
    previous_dir: Path | None


@dataclass(frozen=True, slots=True)
class TitleView:
    entry: LibraryEntry
    slug: str
    record: ProviderRecord | None
    genres: tuple[str, ...]
    cover_name: str | None


def title_slug(entry: LibraryEntry) -> str:
    """Return a display-name-independent, collision-free catalogue slug."""

    return f"title-{entry.id:08x}"


def genre_slug(genre: str) -> str:
    """Return a readable genre slug with a collision-resistant suffix."""

    readable = "-".join(part for part in _slug_words(genre) if part)[:36] or "genre"
    digest = hashlib.sha256(genre.casefold().encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def media_url(relative_path: str) -> str:
    """Build an absolute media route while encoding each unsafe URL character."""

    if relative_path.startswith("/") or "\\" in relative_path:
        raise GenerationError("media path must be canonical and relative")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GenerationError("media path must be canonical and relative")
    return "/media/" + "/".join(quote(part, safe="") for part in parts)


def generate_site(
    repository: CatalogueRepository,
    *,
    site_dir: Path,
    state_dir: Path,
) -> GeneratedSite:
    """Render to staging, validate, and atomically publish a static catalogue."""

    destination = site_dir
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise GenerationError("site_dir must not be a symbolic link")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )
    previous = destination.with_name(f"{destination.name}.previous")
    try:
        page_count, title_count = _render(repository, staging, state_dir)
        _validate(staging)
        retained = _publish(staging, destination, previous)
        return GeneratedSite(title_count, page_count, destination, retained)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _render(
    repository: CatalogueRepository,
    output: Path,
    state_dir: Path,
) -> tuple[int, int]:
    templates = _load_templates()
    (output / "titles").mkdir()
    (output / "genres").mkdir()
    (output / "assets" / "covers").mkdir(parents=True)
    style_name = _copy_versioned_asset("style.css", output / "assets")

    entries = repository.list_library_entries()
    views: list[TitleView] = []
    by_provider_id: dict[tuple[str, str], TitleView] = {}
    genre_members: dict[str, list[TitleView]] = {}
    for entry in entries:
        record = repository.get_provider_record(entry.id, "jikan")
        genres = () if record is None else tuple(repository.list_genres(record.id))
        cover_name = _copy_cover(repository, record, state_dir, output)
        view = TitleView(entry, title_slug(entry), record, genres, cover_name)
        views.append(view)
        if record is not None:
            by_provider_id[(record.provider, record.provider_id)] = view
        for genre in genres:
            genre_members.setdefault(genre, []).append(view)

    scan = repository.get_latest_scan_run()
    scan_status = _scan_status(scan)
    cards = "".join(_card(view) for view in views)
    index_content = templates["index"].substitute(
        title_count=_count_label(len(views), "title"),
        empty_state=(
            '<p class="muted">No local titles have been indexed yet.</p>'
            if not views
            else ""
        ),
        cards=cards,
    )
    _write_page(
        output / "index.html",
        templates,
        page_title="Titles",
        content=index_content,
        root_prefix="",
        asset_prefix="",
        style_name=style_name,
        scan_status=scan_status,
    )

    for view in views:
        _render_title(
            repository,
            view,
            by_provider_id,
            templates,
            output / "titles" / f"{view.slug}.html",
            scan_status,
            style_name,
        )

    sorted_genres = sorted(genre_members, key=str.casefold)
    genre_items = "".join(
        f'<li><a href="{_attr(genre_slug(genre))}.html">'
        f"{_text(genre)}</a> "
        f'<span class="muted">({_text(str(len(genre_members[genre])))})</span></li>'
        for genre in sorted_genres
    )
    genres_content = templates["genres"].substitute(
        empty_state=(
            '<p class="muted">No genres are cached yet.</p>'
            if not sorted_genres
            else ""
        ),
        genres=genre_items,
    )
    _write_page(
        output / "genres" / "index.html",
        templates,
        page_title="Genres",
        content=genres_content,
        root_prefix="../",
        asset_prefix="../",
        style_name=style_name,
        scan_status=scan_status,
    )
    for genre in sorted_genres:
        members = sorted(
            genre_members[genre],
            key=lambda item: (
                item.entry.sort_title.casefold(),
                item.entry.relative_path,
            ),
        )
        title_items = "".join(
            f'<li><a href="../titles/{_attr(view.slug)}.html">'
            f"{_text(view.entry.title)}</a></li>"
            for view in members
        )
        content = templates["genre"].substitute(
            genre=_text(genre),
            title_count=_count_label(len(members), "title"),
            titles=title_items,
        )
        _write_page(
            output / "genres" / f"{genre_slug(genre)}.html",
            templates,
            page_title=genre,
            content=content,
            root_prefix="../",
            asset_prefix="../",
            style_name=style_name,
            scan_status=scan_status,
        )
    return 2 + len(views) + len(sorted_genres), len(views)


def _render_title(
    repository: CatalogueRepository,
    view: TitleView,
    by_provider_id: dict[tuple[str, str], TitleView],
    templates: dict[str, Template],
    destination: Path,
    scan_status: str,
    style_name: str,
) -> None:
    entry, record = view.entry, view.record
    local_files = repository.list_media_files(entry.id)
    local_episode_blocks = []
    for media in local_files:
        url = _attr(media_url(media.relative_path))
        local_episode_blocks.append(
            '<article class="episode">'
            f"<h3>{_text(media.filename)}</h3>"
            f'<video controls preload="metadata" src="{url}">'
            "<p>Your browser cannot play this video. "
            f'<a href="{url}">Download the file</a>.'
            "</p></video></article>"
        )
    local_episodes = "".join(local_episode_blocks) or (
        '<p class="muted">No playable local files are currently available.</p>'
    )

    genres = (
        '<ul class="tag-list" aria-label="Genres">'
        + "".join(
            f'<li><a href="../genres/{_attr(genre_slug(genre))}.html">'
            f"{_text(genre)}</a></li>"
            for genre in view.genres
        )
        + "</ul>"
        if view.genres
        else ""
    )
    provider_episodes = ""
    relations = ""
    if record is not None:
        episodes = repository.list_provider_episodes(record.id)
        if episodes:
            rows = "".join(
                "<tr>"
                f"<td>{episode.episode_number}</td>"
                f"<td>{_text(episode.title or 'Untitled')}</td>"
                f"<td>{_text(_episode_note(episode.filler, episode.recap))}</td>"
                "</tr>"
                for episode in episodes
            )
            provider_episodes = (
                "<section><h2>Provider episode context</h2>"
                '<p class="muted">Reference information only; availability is '
                "shown above.</p><table><thead><tr><th>Episode</th><th>Title</th>"
                f"<th>Notes</th></tr></thead><tbody>{rows}</tbody></table></section>"
            )
        relation_rows = []
        for relation in repository.list_relations(record.id):
            target = by_provider_id.get(
                (relation.target_provider, relation.target_provider_id)
            )
            target_html = _text(relation.target_title)
            if target is not None:
                target_html = f'<a href="{_attr(target.slug)}.html">{target_html}</a>'
            relation_label = _text(relation.relation_type.replace("_", " ").title())
            relation_rows.append(
                f"<li><strong>{relation_label}:</strong> {target_html}</li>"
            )
        if relation_rows:
            relations = (
                '<section><h2>Related titles</h2><ul class="relation-list">'
                + "".join(relation_rows)
                + "</ul></section>"
            )

    cover = (
        f'<img class="cover" src="../assets/covers/{_attr(view.cover_name)}" '
        f'alt="Cover art for {_attr(entry.title)}">'
        if view.cover_name
        else (
            '<div class="placeholder" role="img" '
            'aria-label="No cover art">No cover</div>'
        )
    )
    content = templates["title"].substitute(
        title=_text(entry.title),
        cover=cover,
        metadata_state=("Metadata available" if record else "Unmatched local title"),
        genres=genres,
        synopsis=_text(
            record.synopsis
            if record is not None and record.synopsis
            else "No synopsis is available."
        ),
        local_episodes=local_episodes,
        provider_episodes=provider_episodes,
        relations=relations,
    )
    _write_page(
        destination,
        templates,
        page_title=entry.title,
        content=content,
        root_prefix="../",
        asset_prefix="../",
        style_name=style_name,
        scan_status=scan_status,
    )


def _copy_cover(
    repository: CatalogueRepository,
    record: ProviderRecord | None,
    state_dir: Path,
    output: Path,
) -> str | None:
    if record is None:
        return None
    artwork = repository.get_artwork(record.id, "cover")
    if artwork is None or artwork.relative_path is None:
        return None
    source = _validated_artwork_source(artwork, state_dir)
    if source is None:
        return None
    suffix = source.suffix.casefold()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    name = f"{record.provider}-{record.provider_id}-{digest}{suffix}"
    shutil.copyfile(source, output / "assets" / "covers" / name)
    return name


def _validated_artwork_source(artwork: Artwork, state_dir: Path) -> Path | None:
    if artwork.mime_type not in _IMAGE_TYPES:
        return None
    if artwork.relative_path is None:
        return None
    root = state_dir.resolve()
    source = (state_dir / artwork.relative_path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return None
    size = source.stat().st_size
    if size > MAX_ARTWORK_BYTES:
        return None
    if artwork.size_bytes is not None and size != artwork.size_bytes:
        return None
    return source


def _write_page(
    destination: Path,
    templates: dict[str, Template],
    *,
    page_title: str,
    content: str,
    root_prefix: str,
    asset_prefix: str,
    style_name: str,
    scan_status: str,
) -> None:
    page = templates["base"].substitute(
        page_title=_text(page_title),
        content=content,
        root_prefix=root_prefix,
        asset_prefix=asset_prefix,
        style_name=style_name,
        scan_status=scan_status,
    )
    destination.write_text(page, encoding="utf-8", newline="\n")


def _load_templates() -> dict[str, Template]:
    template_root = resources.files("rpi_streamer").joinpath("templates")
    return {
        name: Template(
            template_root.joinpath(f"{name}.html").read_text(encoding="utf-8")
        )
        for name in _TEMPLATES
    }


def _copy_versioned_asset(name: str, destination: Path) -> str:
    source = resources.files("rpi_streamer").joinpath("static", name)
    content = source.read_bytes()
    source_name = Path(name)
    digest = hashlib.sha256(content).hexdigest()[:16]
    versioned_name = f"{source_name.stem}-{digest}{source_name.suffix}"
    (destination / versioned_name).write_bytes(content)
    return versioned_name


def _validate(output: Path) -> None:
    required = (
        output / "index.html",
        output / "genres" / "index.html",
        output / "titles",
    )
    missing = [str(path.relative_to(output)) for path in required if not path.exists()]
    if missing:
        raise GenerationError(f"generated site is missing: {', '.join(missing)}")
    if not tuple((output / "assets").glob("style-*.css")):
        raise GenerationError("generated site is missing: versioned stylesheet")
    if (
        not (output / "index.html")
        .read_text(encoding="utf-8")
        .startswith("<!doctype html>")
    ):
        raise GenerationError("generated index is not a complete HTML document")


def _publish(staging: Path, site: Path, previous: Path) -> Path | None:
    if previous.is_symlink() or previous.exists():
        if previous.is_symlink() or not previous.is_dir():
            raise GenerationError("previous site path is not a safe directory")
        shutil.rmtree(previous)
    had_site = site.exists()
    if had_site:
        if not site.is_dir():
            raise GenerationError("site_dir exists but is not a directory")
        os.replace(site, previous)
    try:
        os.replace(staging, site)
    except Exception:
        if had_site and previous.exists() and not site.exists():
            os.replace(previous, site)
        raise
    return previous if had_site else None


def _card(view: TitleView) -> str:
    cover = (
        f'<img class="cover" src="assets/covers/{_attr(view.cover_name)}" '
        f'alt="Cover art for {_attr(view.entry.title)}" loading="lazy">'
        if view.cover_name
        else (
            '<div class="placeholder" role="img" '
            'aria-label="No cover art">No cover</div>'
        )
    )
    state = "Metadata cached" if view.record else "Unmatched"
    return (
        '<article class="card">'
        f'<a href="titles/{_attr(view.slug)}.html">{cover}'
        f"<h2>{_text(view.entry.title)}</h2></a>"
        f'<p class="muted">{state}</p></article>'
    )


def _scan_status(scan: ScanRun | None) -> str:
    if scan is None:
        return "No completed scan has been recorded."
    finished = scan.finished_at or scan.started_at
    return (
        f"Last scan: {_text(finished.isoformat(timespec='seconds'))} "
        f"({_text(scan.status)}, {scan.discovered_entries} titles, "
        f"{scan.discovered_files} files, {scan.error_count} errors)."
    )


def _episode_note(filler: bool, recap: bool) -> str:
    notes = [
        label for enabled, label in ((filler, "Filler"), (recap, "Recap")) if enabled
    ]
    return ", ".join(notes) if notes else "—"


def _count_label(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"


def _slug_words(value: str) -> list[str]:
    normalized = "".join(
        char.casefold() if char.isalnum() and char.isascii() else " " for char in value
    )
    return normalized.split()


def _text(value: str) -> str:
    return html.escape(value, quote=False)


def _attr(value: str) -> str:
    return html.escape(value, quote=True)
