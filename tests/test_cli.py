from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_streamer.cli import EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE, main


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

    def test_serve_is_explicitly_unavailable(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(["--config", str(self.config_path), "serve"])
        self.assertEqual(result, EXIT_UNAVAILABLE)
        self.assertIn("implementation milestone", stderr.getvalue())
