from __future__ import annotations

import contextlib
import http.client
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

TEMPLATE = (
    Path(__file__).parents[1] / "deployment" / "nginx" / "rpi-streamer.conf.template"
)
NGINX = shutil.which("nginx")


def render_template(*, listen: str, site: Path, media: Path) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__LISTEN__": listen,
        "__SITE_ROOT__": f"{site}/",
        "__MEDIA_ROOT__": f"{media}/",
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    if any(token in text for token in replacements):
        raise AssertionError("unresolved Nginx template token")
    return text


class NginxTemplateTests(unittest.TestCase):
    def test_template_has_bounded_routes_and_streaming_defaults(self) -> None:
        rendered = render_template(
            listen="192.0.2.10:8080",
            site=Path("/srv/rpi-streamer/site"),
            media=Path("/mnt/anime"),
        )

        self.assertIn("listen 192.0.2.10:8080;", rendered)
        self.assertIn("root /srv/rpi-streamer/site/;", rendered)
        self.assertIn("alias /mnt/anime/$media_file;", rendered)
        self.assertIn("default_type video/mp4;", rendered)
        self.assertIn("disable_symlinks on from=/mnt/anime/;", rendered)
        self.assertIn("location ~ (^|/)\\.", rendered)
        self.assertIn("location /media/", rendered)
        self.assertNotRegex(rendered, r"(?m)^\s*mp4\s")
        self.assertNotIn("autoindex on", rendered)


@unittest.skipUnless(NGINX, "Nginx is not installed")
class NginxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.site = self.root / "site"
        self.media = self.root / "media"
        self.site.mkdir()
        self.media.mkdir()
        (self.site / "index.html").write_text("catalogue", encoding="utf-8")
        (self.site / "assets").mkdir()
        (self.site / "assets" / "style-deadbeef.css").write_text(
            "body{}", encoding="utf-8"
        )
        (self.media / "日本語 title.mp4").write_bytes(bytes(range(256)) * 4)
        (self.media / "notes.txt").write_text("private", encoding="utf-8")
        (self.media / ".hidden.mp4").write_bytes(b"hidden")
        self.port = self._unused_port()
        self.prefix = self.root / "nginx"
        (self.prefix / "logs").mkdir(parents=True)
        site_config = render_template(
            listen=f"127.0.0.1:{self.port}",
            site=self.site,
            media=self.media,
        )
        config = (
            "events {}\nhttp {\n"
            "  access_log off;\n"
            f"  error_log {self.prefix / 'logs/error.log'};\n"
            f"{site_config}\n"
            "}\n"
        )
        self.config = self.prefix / "nginx.conf"
        self.config.write_text(config, encoding="utf-8")
        assert NGINX is not None
        subprocess.run(
            [NGINX, "-t", "-p", f"{self.prefix}/", "-c", str(self.config)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.process = subprocess.Popen(
            [
                NGINX,
                "-p",
                f"{self.prefix}/",
                "-c",
                str(self.config),
                "-g",
                "daemon off;",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop)
        self._wait_until_ready()

    @staticmethod
    def _unused_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                self.fail(f"Nginx exited during startup: {stderr}")
            try:
                status, _, _ = self._request("/healthz")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.02)
        self.fail("Nginx did not become ready")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return (
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def test_catalogue_range_head_conditionals_unicode_and_mime(self) -> None:
        status, headers, body = self._request("/")
        self.assertEqual((status, body), (200, b"catalogue"))
        self.assertEqual(headers["cache-control"], "no-cache")
        modified = headers["last-modified"]

        status, _, body = self._request("/", headers={"If-Modified-Since": modified})
        self.assertEqual((status, body), (304, b""))

        path = "/media/%E6%97%A5%E6%9C%AC%E8%AA%9E%20title.mp4"
        status, headers, body = self._request(path, headers={"Range": "bytes=100-119"})
        self.assertEqual(status, 206)
        self.assertEqual(body, (bytes(range(256)) * 4)[100:120])
        self.assertEqual(headers["content-range"], "bytes 100-119/1024")
        self.assertEqual(headers["content-type"], "video/mp4")

        status, headers, body = self._request(path, method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["accept-ranges"], "bytes")

        status, _, _ = self._request(path, headers={"Range": "bytes=2000-"})
        self.assertEqual(status, 416)

    def test_forbidden_media_and_hidden_paths_are_inaccessible(self) -> None:
        for path in (
            "/media/notes.txt",
            "/media/.hidden.mp4",
            "/media/%2e%2e/site/index.html",
            "/.hidden",
        ):
            with self.subTest(path=path):
                status, _, _ = self._request(path)
                self.assertIn(status, (400, 404))

        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        with contextlib.suppress(OSError):
            os.symlink(outside, self.media / "escape.mp4")
            status, _, _ = self._request("/media/escape.mp4")
            self.assertIn(status, (403, 404))
