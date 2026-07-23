"""Command-line interface for RPi Streamer."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from rpi_streamer.config import (
    ConfigurationError,
    configure_logging,
    load_settings,
)
from rpi_streamer.database import CatalogueRepository
from rpi_streamer.generator import GenerationError, generate_site
from rpi_streamer.metadata import JikanProvider, enrich_catalogue
from rpi_streamer.scanner import scan_library

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3


def build_parser() -> argparse.ArgumentParser:
    """Build the application argument parser."""

    parser = argparse.ArgumentParser(
        prog="rpi-streamer",
        description="Index and serve a local MP4 collection.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "INI configuration path; overrides RPI_STREAMER_CONFIG and "
            "/etc/rpi-streamer/rpi-streamer.ini"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the periodic indexing service")
    subparsers.add_parser("scan", help="perform one scan and exit")
    subparsers.add_parser(
        "validate-config",
        help="validate and print the normalized configuration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(config_path=args.config)
    except ConfigurationError as error:
        print(f"rpi-streamer: configuration error: {error}", file=sys.stderr)
        return EXIT_USAGE

    configure_logging(settings.log_level)
    if args.command == "validate-config":
        print(settings.to_json())
        return EXIT_OK
    if args.command == "scan":
        with CatalogueRepository(settings.database_path) as repository:
            enrich = None
            if settings.metadata_provider == "jikan":
                provider = JikanProvider()

                def enrich(
                    repository: CatalogueRepository, scanned_at: datetime
                ) -> tuple[str, ...]:
                    return enrich_catalogue(
                        repository,
                        provider,
                        refresh_interval=settings.metadata_refresh_interval,
                        state_dir=settings.state_dir,
                        download_artwork=settings.download_artwork,
                        metadata_language=settings.metadata_language,
                        now=scanned_at,
                    ).errors

            result = scan_library(
                repository,
                settings.media_root,
                enrich=enrich,
            )
            try:
                generated = generate_site(
                    repository,
                    site_dir=settings.site_dir,
                    state_dir=settings.state_dir,
                )
            except (GenerationError, OSError) as error:
                print(
                    f"rpi-streamer: catalogue generation failed: {error}",
                    file=sys.stderr,
                )
                return EXIT_UNAVAILABLE
        print(
            f"scan {result.status}: {result.discovered_entries} title(s), "
            f"{result.discovered_files} file(s), {result.error_count} error(s); "
            f"generated {generated.page_count} page(s)"
        )
        return EXIT_OK if result.status == "success" else EXIT_UNAVAILABLE

    print(
        f"rpi-streamer: {args.command} is not available until its "
        "implementation milestone is complete",
        file=sys.stderr,
    )
    return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
