"""Command-line interface for RPi Streamer."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from rpi_streamer.candidates import WorkVerifier
from rpi_streamer.config import (
    ConfigurationError,
    Settings,
    configure_logging,
    load_settings,
)
from rpi_streamer.database import CatalogueRepository, DatabaseError, ProviderRecord
from rpi_streamer.metadata import (
    JikanProvider,
    ProviderError,
    TenraiProvider,
    verify_provider_work,
)
from rpi_streamer.nginx import render_nginx, write_nginx
from rpi_streamer.operator import (
    CollectionNotFoundError,
    inspect_collection,
    invalidate_model,
    recompute_deterministic,
    refresh_candidates,
    validate_collection_sidecar,
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
    subparsers.add_parser(
        "healthcheck",
        help="check whether the long-running indexer is healthy",
    )
    nginx_parser = subparsers.add_parser(
        "render-nginx",
        help="render Nginx configuration from resolved settings",
    )
    nginx_parser.add_argument("--listen", default="127.0.0.1:8080")
    nginx_parser.add_argument("--output", type=Path, required=True)
    mapping_parser = subparsers.add_parser(
        "mapping",
        help="inspect or control one collection's multi-work mappings",
    )
    mapping_subparsers = mapping_parser.add_subparsers(
        dest="mapping_command", required=True
    )
    for command, help_text in (
        ("inspect", "print bounded mapping diagnostics"),
        ("validate-sidecar", "dry-run the collection sidecar"),
        ("refresh-candidates", "refresh the bounded relation candidates"),
        ("invalidate-model", "remove only model mappings and exact caches"),
        ("recompute", "recompute deterministic mappings"),
    ):
        command_parser = mapping_subparsers.add_parser(command, help=help_text)
        command_parser.add_argument(
            "collection", help="exact collection path relative to media_root"
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
    if args.command == "healthcheck":
        return _healthcheck(settings.state_dir / "status.json")
    if args.command == "render-nginx":
        try:
            write_nginx(args.output, render_nginx(settings, args.listen))
        except (OSError, ValueError) as error:
            print(f"rpi-streamer: cannot render Nginx config: {error}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    if args.command == "mapping":
        return _mapping_command(settings, args.mapping_command, args.collection)
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


def _mapping_command(settings: Settings, command: str, collection: str) -> int:
    """Run one bounded collection mapping operation."""

    database_path = settings.database_path
    media_root = settings.media_root
    try:
        lock = (
            InstanceLock(settings.state_dir)
            if command in {"refresh-candidates", "invalidate-model", "recompute"}
            else contextlib.nullcontext()
        )
        with lock, CatalogueRepository(database_path) as repository:
            if command == "inspect":
                payload = inspect_collection(repository, media_root, collection)
            elif command == "validate-sidecar":
                payload = validate_collection_sidecar(
                    repository, media_root, collection
                )
            elif command == "invalidate-model":
                payload = invalidate_model(repository, collection)
            elif command == "recompute":
                payload = recompute_deterministic(repository, collection)
            else:
                payload = refresh_candidates(
                    repository,
                    collection,
                    verify_work=_work_verifier(settings),
                )
    except CollectionNotFoundError as error:
        print(f"rpi-streamer: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ValueError as error:
        print(f"rpi-streamer: invalid sidecar: {error}", file=sys.stderr)
        return EXIT_USAGE
    except AlreadyRunningError as error:
        print(f"rpi-streamer: {error}", file=sys.stderr)
        return EXIT_LOCKED
    except (OSError, DatabaseError, ProviderError) as error:
        print(f"rpi-streamer: mapping operation failed: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def _work_verifier(settings: Settings) -> WorkVerifier | None:
    if settings.metadata_provider not in {"jikan", "tenrai"}:
        return None
    provider = (
        TenraiProvider() if settings.metadata_provider == "tenrai" else JikanProvider()
    )

    def verify(
        repository: CatalogueRepository,
        provider_id: str,
        verified_at: datetime,
    ) -> tuple[ProviderRecord | None, str | None]:
        try:
            return (
                verify_provider_work(
                    repository,
                    provider,
                    provider_id,
                    metadata_language=settings.metadata_language,
                    now=verified_at,
                ),
                None,
            )
        except (ProviderError, ValueError, OSError) as error:
            return None, str(error)

    return verify


def _healthcheck(path: Path) -> int:
    """Check the atomic service status and its owning process."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        state = payload["state"]
        if not isinstance(pid, int) or pid <= 0 or state not in {"ready", "scanning"}:
            return EXIT_UNAVAILABLE
        os.kill(pid, 0)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return EXIT_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
