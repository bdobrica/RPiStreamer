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
from rpi_streamer.service import AlreadyRunningError, InstanceLock, Service, run_once

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_LOCKED = 4


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
    scan_parser = subparsers.add_parser("scan", help="perform one scan and exit")
    scan_parser.add_argument(
        "--json",
        action="store_true",
        help="print the scan summary as one JSON object",
    )
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
        try:
            with InstanceLock(settings.state_dir):
                result = run_once(settings)
        except AlreadyRunningError as error:
            print(f"rpi-streamer: {error}", file=sys.stderr)
            return EXIT_LOCKED
        except Exception as error:
            print(f"rpi-streamer: scan failed: {error}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        if args.json:
            print(result.to_json())
        else:
            print(
                f"scan {result.status}: {result.discovered_entries} title(s), "
                f"{result.discovered_files} file(s), {result.error_count} error(s); "
                f"generated {result.generated_pages} page(s)"
            )
        return EXIT_OK if result.status == "success" else EXIT_UNAVAILABLE
    if args.command == "serve":
        try:
            return Service(settings).run()
        except AlreadyRunningError as error:
            print(f"rpi-streamer: {error}", file=sys.stderr)
            return EXIT_LOCKED
        except Exception as error:
            print(f"rpi-streamer: service failed: {error}", file=sys.stderr)
            return EXIT_UNAVAILABLE

    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
