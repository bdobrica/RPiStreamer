"""Long-running scan orchestration, process locking, and status reporting."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Final, TextIO, cast

from rpi_streamer.config import Settings
from rpi_streamer.database import CatalogueRepository, ScanRun
from rpi_streamer.generator import GeneratedSite, generate_site
from rpi_streamer.metadata import JikanProvider, enrich_catalogue
from rpi_streamer.scanner import scan_library

LOGGER = logging.getLogger(__name__)
LOCK_NAME: Final = "indexer.lock"
STATUS_NAME: Final = "status.json"


class AlreadyRunningError(RuntimeError):
    """Raised when another indexer owns the state-directory lock."""


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Machine-readable result of a complete scan and generation cycle."""

    scan_id: int
    status: str
    discovered_entries: int
    discovered_files: int
    error_count: int
    generated_pages: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def run_once(settings: Settings) -> RunSummary:
    """Scan, enrich, and atomically publish one catalogue generation."""

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

        result = scan_library(repository, settings.media_root, enrich=enrich)
        generated = generate_site(
            repository,
            site_dir=settings.site_dir,
            state_dir=settings.state_dir,
        )
    return _summary(result, generated)


def _summary(scan: ScanRun, generated: GeneratedSite) -> RunSummary:
    return RunSummary(
        scan_id=scan.id,
        status=scan.status,
        discovered_entries=scan.discovered_entries,
        discovered_files=scan.discovered_files,
        error_count=scan.error_count,
        generated_pages=generated.page_count,
    )


class InstanceLock(AbstractContextManager["InstanceLock"]):
    """Advisory, automatically released single-instance lock."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / LOCK_NAME
        self._stream: TextIO | None = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="ascii")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.close()
            raise AlreadyRunningError(
                f"another indexer is using state directory {self.path.parent}"
            ) from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        self._stream = stream
        return self

    def __exit__(self, *_args: object) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class Service:
    """Run scans serially in response to startup, time, and signals."""

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Callable[[Settings], RunSummary] = run_once,
        monotonic: Callable[[], float] = time.monotonic,
        event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.monotonic = monotonic
        self.event = threading.Event() if event is None else event
        self.stop_requested = False
        self.scan_requested = False
        self._old_handlers: dict[int, signal.Handlers] = {}

    def request_scan(
        self, _signum: int | None = None, _frame: FrameType | None = None
    ) -> None:
        """Coalesce any number of rescan requests into one pending scan."""

        self.scan_requested = True
        self.event.set()

    def request_stop(
        self, _signum: int | None = None, _frame: FrameType | None = None
    ) -> None:
        """Request shutdown after the current atomic scan cycle."""

        self.stop_requested = True
        self.event.set()

    def run(self) -> int:
        """Run until stopped, returning success after graceful shutdown."""

        self._install_handlers()
        try:
            with InstanceLock(self.settings.state_dir):
                self._write_status("starting")
                next_scan = self.monotonic()
                while not self.stop_requested:
                    self.event.clear()
                    now = self.monotonic()
                    if self.scan_requested or now >= next_scan:
                        self.scan_requested = False
                        self._perform_scan()
                        if self.settings.scan_interval == 0:
                            next_scan = float("inf")
                        else:
                            next_scan = self.monotonic() + self.settings.scan_interval
                        continue
                    timeout = (
                        None if math.isinf(next_scan) else max(0.0, next_scan - now)
                    )
                    self.event.wait(timeout=timeout)
                self._write_status("stopped")
                LOGGER.info("event=service_stopped")
                return 0
        finally:
            self._restore_handlers()

    def _perform_scan(self) -> None:
        started_at = datetime.now(UTC)
        self._write_status("scanning", started_at=started_at)
        LOGGER.info("event=scan_started")
        try:
            summary = self.runner(self.settings)
        except Exception as error:
            message = _safe_log_value(error)
            LOGGER.error("event=scan_failed error=%s", message)
            self._write_status("degraded", started_at=started_at, error=message)
            return
        LOGGER.info(
            "event=scan_finished scan_id=%d status=%s titles=%d files=%d "
            "errors=%d pages=%d",
            summary.scan_id,
            summary.status,
            summary.discovered_entries,
            summary.discovered_files,
            summary.error_count,
            summary.generated_pages,
        )
        self._write_status(
            "ready",
            started_at=started_at,
            summary=summary,
        )

    def _write_status(
        self,
        state: str,
        *,
        started_at: datetime | None = None,
        summary: RunSummary | None = None,
        error: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pid": os.getpid(),
            "state": state,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if started_at is not None:
            payload["scan_started_at"] = started_at.isoformat()
        if summary is not None:
            payload["last_scan"] = asdict(summary)
        if error is not None:
            payload["error"] = error
        write_json_atomic(self.settings.state_dir / STATUS_NAME, payload)

    def _install_handlers(self) -> None:
        for signum, handler in (
            (signal.SIGHUP, self.request_scan),
            (signal.SIGINT, self.request_stop),
            (signal.SIGTERM, self.request_stop),
        ):
            self._old_handlers[signum] = cast(signal.Handlers, signal.getsignal(signum))
            signal.signal(signum, handler)

    def _restore_handlers(self) -> None:
        for signum, handler in self._old_handlers.items():
            signal.signal(signum, handler)
        self._old_handlers.clear()


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Replace a JSON artifact without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_log_value(value: object) -> str:
    text = str(value)
    return "".join(character if character.isprintable() else " " for character in text)[
        :500
    ]
