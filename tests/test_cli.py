from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.cli import EXIT_LOCKED, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE, main
from rpi_streamer.service import AlreadyRunningError, RunSummary


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        media_root = root / "media"
        media_root.mkdir()
        self.config_path = root / "config.ini"
        self.config_path.write_text(
            "\n".join(
                (
                    "[rpi-streamer]",
                    f"media_root = {media_root}",
                    f"state_dir = {root / 'state'}",
                    f"site_dir = {root / 'state' / 'site'}",
                    f"database_path = {root / 'state' / 'catalogue.db'}",
                    "metadata_provider = none",
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def test_validate_config_prints_normalized_json(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            result = main(["--config", str(self.config_path), "validate-config"])

        self.assertEqual(result, EXIT_OK)
        self.assertIn('"scan_interval": 3600', stdout.getvalue())
        self.assertNotIn("RPI_STREAMER_", stdout.getvalue())

    def test_invalid_config_returns_usage_error(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(["--config", str(self.config_path) + ".missing", "scan"])

        self.assertEqual(result, EXIT_USAGE)
        self.assertIn("configuration error", stderr.getvalue())

    def test_scan_runs_and_prints_summary(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            result = main(["--config", str(self.config_path), "scan"])

        self.assertEqual(result, EXIT_OK)
        self.assertIn("scan success: 0 title(s), 0 file(s)", stdout.getvalue())

    def test_scan_can_print_json_summary(self) -> None:
        stdout = io.StringIO()
        expected = RunSummary(3, "success", 1, 2, 0, 4)
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("rpi_streamer.cli.run_once", return_value=expected),
            contextlib.redirect_stdout(stdout),
        ):
            result = main(["--config", str(self.config_path), "scan", "--json"])
        self.assertEqual(result, EXIT_OK)
        self.assertIn('"scan_id": 3', stdout.getvalue())

    def test_serve_reports_lock_contention(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "rpi_streamer.cli.Service.run",
                side_effect=AlreadyRunningError("already running"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(["--config", str(self.config_path), "serve"])
        self.assertEqual(result, EXIT_LOCKED)
        self.assertIn("already running", stderr.getvalue())

    def test_serve_reports_operational_failure(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "rpi_streamer.cli.Service.run",
                side_effect=OSError("status unavailable"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(["--config", str(self.config_path), "serve"])
        self.assertEqual(result, EXIT_UNAVAILABLE)
        self.assertIn("service failed", stderr.getvalue())

    def test_healthcheck_accepts_live_ready_service(self) -> None:
        state = self.config_path.parent / "state"
        state.mkdir()
        (state / "status.json").write_text(
            json.dumps({"pid": os.getpid(), "state": "ready"}),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {}, clear=True):
            result = main(["--config", str(self.config_path), "healthcheck"])
        self.assertEqual(result, EXIT_OK)

    def test_healthcheck_rejects_missing_malformed_or_degraded_status(self) -> None:
        state = self.config_path.parent / "state"
        state.mkdir()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                main(["--config", str(self.config_path), "healthcheck"]),
                EXIT_UNAVAILABLE,
            )
            (state / "status.json").write_text("{", encoding="utf-8")
            self.assertEqual(
                main(["--config", str(self.config_path), "healthcheck"]),
                EXIT_UNAVAILABLE,
            )
            (state / "status.json").write_text(
                json.dumps({"pid": os.getpid(), "state": "degraded"}),
                encoding="utf-8",
            )
            self.assertEqual(
                main(["--config", str(self.config_path), "healthcheck"]),
                EXIT_UNAVAILABLE,
            )

    def test_mapping_inspect_is_deterministic_and_sidecar_validation_is_dry_run(
        self,
    ) -> None:
        collection = self.config_path.parent / "media" / "Show"
        collection.mkdir()
        (collection / "01 <pilot>.mp4").write_bytes(b"video")
        (collection / "rpi-streamer.ini").write_text(
            "[rpi-streamer]\ndisplay_title = Show\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["--config", str(self.config_path), "scan"]), EXIT_OK)
            outputs = []
            for _ in range(2):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = main(
                        [
                            "--config",
                            str(self.config_path),
                            "mapping",
                            "inspect",
                            "Show",
                        ]
                    )
                self.assertEqual(result, EXIT_OK)
                outputs.append(stdout.getvalue())
            validated = io.StringIO()
            with contextlib.redirect_stdout(validated):
                validation_result = main(
                    [
                        "--config",
                        str(self.config_path),
                        "mapping",
                        "validate-sidecar",
                        "Show",
                    ]
                )

        self.assertEqual(validation_result, EXIT_OK)
        self.assertEqual(outputs[0], outputs[1])
        payload = json.loads(outputs[0])
        self.assertEqual(payload["collection"], "Show")
        self.assertEqual(payload["files"][0]["filename"], "01 <pilot>.mp4")
        self.assertNotIn("digest", outputs[0])
        self.assertNotIn("api_key", outputs[0])
        self.assertEqual(json.loads(validated.getvalue())["status"], "valid")

    def test_mapping_commands_report_unknown_collection_and_invalid_sidecar(
        self,
    ) -> None:
        collection = self.config_path.parent / "media" / "Broken"
        collection.mkdir()
        (collection / "01.mp4").write_bytes(b"video")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["--config", str(self.config_path), "scan"]), EXIT_OK)
            unknown_error = io.StringIO()
            with contextlib.redirect_stderr(unknown_error):
                unknown = main(
                    [
                        "--config",
                        str(self.config_path),
                        "mapping",
                        "inspect",
                        "Missing",
                    ]
                )
            (collection / "rpi-streamer.ini").write_text(
                "[rpi-streamer]\nunknown = secret-value\n",
                encoding="utf-8",
            )
            invalid_error = io.StringIO()
            with contextlib.redirect_stderr(invalid_error):
                invalid = main(
                    [
                        "--config",
                        str(self.config_path),
                        "mapping",
                        "validate-sidecar",
                        "Broken",
                    ]
                )

        self.assertEqual(unknown, EXIT_UNAVAILABLE)
        self.assertIn("unknown collection", unknown_error.getvalue())
        self.assertEqual(invalid, EXIT_USAGE)
        self.assertIn("invalid sidecar", invalid_error.getvalue())
        self.assertNotIn("secret-value", invalid_error.getvalue())
