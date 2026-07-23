from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from rpi_streamer.config import Settings
from rpi_streamer.service import (
    STATUS_NAME,
    AlreadyRunningError,
    InstanceLock,
    RunSummary,
    Service,
    write_json_atomic,
)


def summary(scan_id: int = 1, status: str = "success") -> RunSummary:
    return RunSummary(scan_id, status, 2, 3, 0, 5)


class FakeEvent:
    def __init__(self, clock: list[float], waits: list[float]) -> None:
        self.clock = clock
        self.waits = waits
        self.service: Service | None = None

    def set(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float("inf") if timeout is None else timeout)
        if timeout is not None:
            self.clock[0] += timeout
        assert self.service is not None
        self.service.request_stop()
        return False


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        media = root / "media"
        media.mkdir()
        self.settings = Settings(
            media_root=media,
            state_dir=root / "state",
            site_dir=root / "state" / "site",
            database_path=root / "state" / "catalogue.db",
            scan_interval=30,
            metadata_provider="none",
        )

    def test_startup_scan_then_monotonic_wait(self) -> None:
        clock = [10.0]
        waits: list[float] = []
        event = FakeEvent(clock, waits)
        calls: list[int] = []

        def runner(_settings: Settings) -> RunSummary:
            calls.append(1)
            return summary()

        service = Service(
            self.settings,
            runner=runner,
            monotonic=lambda: clock[0],
            event=event,  # type: ignore[arg-type]
        )
        event.service = service
        with (
            patch.object(service, "_install_handlers"),
            patch.object(service, "_restore_handlers"),
        ):
            result = service.run()

        self.assertEqual(result, 0)
        self.assertEqual(calls, [1])
        self.assertEqual(waits, [30.0])
        status = json.loads((self.settings.state_dir / STATUS_NAME).read_text())
        self.assertEqual(status["state"], "stopped")

    def test_zero_interval_disables_scheduling(self) -> None:
        settings = Settings(
            media_root=self.settings.media_root,
            state_dir=self.settings.state_dir,
            site_dir=self.settings.site_dir,
            database_path=self.settings.database_path,
            scan_interval=0,
            metadata_provider="none",
        )
        clock = [10.0]
        waits: list[float] = []
        event = FakeEvent(clock, waits)
        calls: list[int] = []

        def runner(_settings: Settings) -> RunSummary:
            calls.append(1)
            return summary()

        service = Service(
            settings,
            runner=runner,
            monotonic=lambda: clock[0],
            event=event,  # type: ignore[arg-type]
        )
        event.service = service
        with (
            patch.object(service, "_install_handlers"),
            patch.object(service, "_restore_handlers"),
        ):
            service.run()
        self.assertEqual(calls, [1])
        self.assertEqual(waits, [float("inf")])

    def test_signal_requests_are_coalesced_and_follow_active_scan(self) -> None:
        calls: list[int] = []
        service: Service

        def runner(_settings: Settings) -> RunSummary:
            calls.append(1)
            if len(calls) == 1:
                service.request_scan()
                service.request_scan()
            else:
                service.request_stop()
            return summary(len(calls))

        service = Service(self.settings, runner=runner)
        with (
            patch.object(service, "_install_handlers"),
            patch.object(service, "_restore_handlers"),
        ):
            service.run()
        self.assertEqual(calls, [1, 1])

    def test_failed_scan_recovers_on_requested_followup(self) -> None:
        calls = 0
        service: Service

        def runner(_settings: Settings) -> RunSummary:
            nonlocal calls
            calls += 1
            if calls == 1:
                service.request_scan()
                raise RuntimeError("bad\nvalue")
            service.request_stop()
            return summary(2)

        service = Service(self.settings, runner=runner)
        with (
            patch.object(service, "_install_handlers"),
            patch.object(service, "_restore_handlers"),
        ):
            service.run()
        self.assertEqual(calls, 2)

    def test_lock_contention_and_release(self) -> None:
        with (
            InstanceLock(self.settings.state_dir),
            self.assertRaises(AlreadyRunningError),
            InstanceLock(self.settings.state_dir),
        ):
            pass
        with InstanceLock(self.settings.state_dir):
            pass

    def test_handlers_map_signals_and_are_restored(self) -> None:
        service = Service(self.settings)
        with patch("rpi_streamer.service.signal.signal") as install:
            service._install_handlers()
            service._restore_handlers()
        installed = {call.args[0]: call.args[1] for call in install.call_args_list[:3]}
        self.assertEqual(installed[signal.SIGHUP], service.request_scan)
        self.assertEqual(installed[signal.SIGINT], service.request_stop)
        self.assertEqual(installed[signal.SIGTERM], service.request_stop)

    def test_atomic_status_is_valid_json(self) -> None:
        target = self.settings.state_dir / STATUS_NAME
        write_json_atomic(target, {"state": "ready", "pid": 12})
        self.assertEqual(json.loads(target.read_text())["state"], "ready")
        self.assertEqual(list(target.parent.glob(f".{STATUS_NAME}.*")), [])

    def test_request_stop_wakes_waiter(self) -> None:
        event = threading.Event()
        service = Service(self.settings, event=event)
        service.request_stop()
        self.assertTrue(event.is_set())

    def test_process_hup_rescans_and_term_stops(self) -> None:
        config = self.settings.state_dir.parent / "service.ini"
        config.write_text(
            "\n".join(
                (
                    "[rpi-streamer]",
                    f"media_root = {self.settings.media_root}",
                    f"state_dir = {self.settings.state_dir}",
                    f"site_dir = {self.settings.site_dir}",
                    f"database_path = {self.settings.database_path}",
                    "scan_interval = 0",
                    "metadata_provider = none",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "rpi_streamer",
                "--config",
                str(config),
                "serve",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_process, process)
        status_path = self.settings.state_dir / STATUS_NAME
        first = self._wait_for_status(status_path, "ready")
        first_scan = cast(dict[str, object], first["last_scan"])
        first_id = cast(int, first_scan["scan_id"])

        process.send_signal(signal.SIGHUP)
        second = self._wait_for_scan_after(status_path, first_id)
        second_scan = cast(dict[str, object], second["last_scan"])
        self.assertGreater(cast(int, second_scan["scan_id"]), first_id)

        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(json.loads(status_path.read_text())["state"], "stopped")

    @staticmethod
    def _wait_for_status(path: Path, state: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                payload = cast(dict[str, object], json.loads(path.read_text()))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            if payload.get("state") == state:
                return payload
            time.sleep(0.02)
        raise AssertionError(f"status did not become {state}")

    @staticmethod
    def _wait_for_scan_after(path: Path, scan_id: int) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = cast(dict[str, object], json.loads(path.read_text()))
            last_scan = payload.get("last_scan")
            if (
                payload.get("state") == "ready"
                and isinstance(last_scan, dict)
                and last_scan.get("scan_id", 0) > scan_id
            ):
                return payload
            time.sleep(0.02)
        raise AssertionError("follow-up scan did not finish")

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
