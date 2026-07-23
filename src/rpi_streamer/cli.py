"""Command-line interface for RPi Streamer."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rpi_streamer.config import (
    ConfigurationError,
    configure_logging,
    load_settings,
)

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

    print(
        f"rpi-streamer: {args.command} is not available until its "
        "implementation milestone is complete",
        file=sys.stderr,
    )
    return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
