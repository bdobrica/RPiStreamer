from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rpi_streamer.config import Settings
from rpi_streamer.nginx import render_nginx, write_nginx


class NginxRenderTests(unittest.TestCase):
    def test_uses_resolved_media_and_site_paths(self) -> None:
        settings = Settings(media_root=Path("/mnt/media"), site_dir=Path("/srv/site"))

        rendered = render_nginx(settings, "192.168.11.111:80")

        self.assertIn("listen 192.168.11.111:80;", rendered)
        self.assertIn('alias "/mnt/media/$media_file";', rendered)
        self.assertIn("disable_symlinks on from=/mnt/media/;", rendered)
        self.assertIn('root "/srv/site/";', rendered)
        self.assertNotIn("/mnt/anime", rendered)

    def test_spaces_are_quoted_and_unsafe_paths_are_rejected(self) -> None:
        rendered = render_nginx(
            Settings(media_root=Path("/mnt/my anime")), "127.0.0.1:8080"
        )
        self.assertIn('alias "/mnt/my anime/$media_file";', rendered)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            render_nginx(Settings(media_root=Path("/mnt/bad$root")), "127.0.0.1:8080")

    def test_atomic_writer_replaces_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "site.conf"
            output.write_text("old")
            write_nginx(output, "new\n")
            self.assertEqual(output.read_text(), "new\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
