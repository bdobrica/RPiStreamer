from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rpi_streamer.config import (
    ConfigurationError,
    load_settings,
    parse_bool,
    parse_duration,
)


class SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.state_dir = self.root / "state"
        self.site_dir = self.state_dir / "site"
        self.database_path = self.state_dir / "catalogue.db"
        self.base_environment = self._environment()

    def _environment(self, **overrides: str) -> dict[str, str]:
        environment = {
            "RPI_STREAMER_MEDIA_ROOT": str(self.media_root),
            "RPI_STREAMER_STATE_DIR": str(self.state_dir),
            "RPI_STREAMER_SITE_DIR": str(self.site_dir),
            "RPI_STREAMER_DATABASE_PATH": str(self.database_path),
        }
        environment.update(overrides)
        return environment

    def _write_config(self, **overrides: str) -> Path:
        values = {
            "media_root": str(self.media_root),
            "state_dir": str(self.state_dir),
            "site_dir": str(self.site_dir),
            "database_path": str(self.database_path),
            "scan_interval": "1h",
            "metadata_provider": "jikan",
            "metadata_refresh_interval": "30d",
            "metadata_language": "en",
            "download_artwork": "true",
            "log_level": "INFO",
        }
        values.update(overrides)
        config_path = self.root / "rpi-streamer.ini"
        lines = ["[rpi-streamer]"]
        lines.extend(f"{key} = {value}" for key, value in values.items())
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config_path

    def test_defaults_are_used_when_default_ini_is_absent(self) -> None:
        settings = load_settings(environ=self.base_environment)

        self.assertEqual(settings.scan_interval, 3600)
        self.assertEqual(settings.metadata_refresh_interval, 30 * 86400)
        self.assertEqual(settings.metadata_provider, "jikan")
        self.assertTrue(settings.download_artwork)
        self.assertEqual(settings.log_level, "INFO")

    def test_ini_values_are_loaded(self) -> None:
        path = self._write_config(
            scan_interval="15m",
            metadata_provider="none",
            metadata_refresh_interval="2d",
            metadata_language="ro",
            download_artwork="off",
            log_level="debug",
        )

        settings = load_settings(config_path=path, environ={})

        self.assertEqual(settings.scan_interval, 900)
        self.assertEqual(settings.metadata_provider, "none")
        self.assertEqual(settings.metadata_refresh_interval, 172800)
        self.assertEqual(settings.metadata_language, "ro")
        self.assertFalse(settings.download_artwork)
        self.assertEqual(settings.log_level, "DEBUG")

    def test_environment_overrides_ini(self) -> None:
        path = self._write_config(scan_interval="5m", log_level="WARNING")

        settings = load_settings(
            config_path=path,
            environ=self._environment(
                RPI_STREAMER_SCAN_INTERVAL="20m",
                RPI_STREAMER_LOG_LEVEL="ERROR",
            ),
        )

        self.assertEqual(settings.scan_interval, 1200)
        self.assertEqual(settings.log_level, "ERROR")

    def test_every_setting_has_an_environment_override(self) -> None:
        alternate_media = self.root / "alternate-media"
        alternate_media.mkdir()
        alternate_state = self.root / "alternate-state"
        environment = {
            "RPI_STREAMER_MEDIA_ROOT": str(alternate_media),
            "RPI_STREAMER_STATE_DIR": str(alternate_state),
            "RPI_STREAMER_SITE_DIR": str(alternate_state / "web"),
            "RPI_STREAMER_DATABASE_PATH": str(alternate_state / "library.sqlite"),
            "RPI_STREAMER_SCAN_INTERVAL": "45m",
            "RPI_STREAMER_METADATA_PROVIDER": "none",
            "RPI_STREAMER_METADATA_REFRESH_INTERVAL": "12h",
            "RPI_STREAMER_METADATA_LANGUAGE": "ja",
            "RPI_STREAMER_DOWNLOAD_ARTWORK": "no",
            "RPI_STREAMER_LOG_LEVEL": "warning",
        }

        settings = load_settings(environ=environment)

        self.assertEqual(settings.media_root, alternate_media)
        self.assertEqual(settings.state_dir, alternate_state)
        self.assertEqual(settings.site_dir, alternate_state / "web")
        self.assertEqual(settings.database_path, alternate_state / "library.sqlite")
        self.assertEqual(settings.scan_interval, 2700)
        self.assertEqual(settings.metadata_provider, "none")
        self.assertEqual(settings.metadata_refresh_interval, 43200)
        self.assertEqual(settings.metadata_language, "ja")
        self.assertFalse(settings.download_artwork)
        self.assertEqual(settings.log_level, "WARNING")

    def test_environment_selects_config_file(self) -> None:
        path = self._write_config(scan_interval="8m")

        settings = load_settings(environ={"RPI_STREAMER_CONFIG": str(path)})

        self.assertEqual(settings.scan_interval, 480)

    def test_cli_config_path_takes_precedence_over_environment_path(self) -> None:
        selected = self._write_config(scan_interval="7m")

        settings = load_settings(
            config_path=selected,
            environ=self._environment(
                RPI_STREAMER_CONFIG=str(self.root / "missing.ini")
            ),
        )

        self.assertEqual(settings.scan_interval, 420)

    def test_explicit_missing_config_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "not found"):
            load_settings(
                config_path=self.root / "missing.ini",
                environ=self.base_environment,
            )

    def test_unknown_option_is_rejected(self) -> None:
        path = self._write_config(typo="value")

        with self.assertRaisesRegex(ConfigurationError, "unknown.*typo"):
            load_settings(config_path=path, environ={})

    def test_missing_section_is_rejected(self) -> None:
        path = self.root / "bad.ini"
        path.write_text("[other]\nvalue = x\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigurationError, r"\[rpi-streamer\]"):
            load_settings(config_path=path, environ={})

    def test_relative_path_is_rejected(self) -> None:
        environment = self._environment(RPI_STREAMER_MEDIA_ROOT="relative")

        with self.assertRaisesRegex(ConfigurationError, "absolute"):
            load_settings(environ=environment)

    def test_missing_media_root_is_rejected(self) -> None:
        environment = self._environment(
            RPI_STREAMER_MEDIA_ROOT=str(self.root / "missing")
        )

        with self.assertRaisesRegex(ConfigurationError, "not a directory"):
            load_settings(environ=environment)

    def test_state_inside_media_root_is_rejected(self) -> None:
        state = self.media_root / "state"
        environment = self._environment(
            RPI_STREAMER_STATE_DIR=str(state),
            RPI_STREAMER_SITE_DIR=str(state / "site"),
            RPI_STREAMER_DATABASE_PATH=str(state / "catalogue.db"),
        )

        with self.assertRaisesRegex(ConfigurationError, "inside media_root"):
            load_settings(environ=environment)

    def test_equal_paths_are_rejected(self) -> None:
        environment = self._environment(RPI_STREAMER_SITE_DIR=str(self.state_dir))

        with self.assertRaisesRegex(ConfigurationError, "must be distinct"):
            load_settings(environ=environment)

    def test_invalid_choice_values_are_rejected(self) -> None:
        cases = {
            "RPI_STREAMER_METADATA_PROVIDER": "mal",
            "RPI_STREAMER_METADATA_LANGUAGE": "",
            "RPI_STREAMER_LOG_LEVEL": "TRACE",
            "RPI_STREAMER_METADATA_REFRESH_INTERVAL": "0",
        }
        for variable, value in cases.items():
            with (
                self.subTest(variable=variable),
                self.assertRaises(ConfigurationError),
            ):
                load_settings(environ=self._environment(**{variable: value}))

    def test_serialized_settings_are_normalized_and_sorted(self) -> None:
        settings = load_settings(environ=self.base_environment)

        output = settings.to_json()

        self.assertIn('"scan_interval": 3600', output)
        self.assertLess(output.index("database_path"), output.index("state_dir"))


class ParserTestCase(unittest.TestCase):
    def test_duration_values(self) -> None:
        cases = {
            "0": 0,
            "15s": 15,
            "2m": 120,
            "3H": 10800,
            "4d": 345600,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_duration(value, name="duration"), expected)

    def test_invalid_duration_values(self) -> None:
        for value in ("", "-1", "1.5h", "1w", " 2 h", "01m"):
            with (
                self.subTest(value=value),
                self.assertRaises(ConfigurationError),
            ):
                parse_duration(value, name="duration")

    def test_boolean_values(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool(value, name="flag"))
        for value in ("0", "false", "FALSE", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(parse_bool(value, name="flag"))

    def test_invalid_boolean_value(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_bool("enabled", name="flag")
