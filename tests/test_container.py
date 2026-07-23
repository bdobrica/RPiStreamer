from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "compose.yaml"
INDEXER = ROOT / "Dockerfile"
NGINX = ROOT / "deployment" / "container" / "nginx.Dockerfile"
SITE = ROOT / "deployment" / "container" / "site.conf"
BAKE = ROOT / "docker-bake.hcl"


class ContainerAssetTests(unittest.TestCase):
    def test_compose_mounts_and_hardening_are_explicit(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("source: ${RPI_STREAMER_MEDIA_PATH:-./media}", text)
        self.assertEqual(text.count("target: /media"), 2)
        self.assertEqual(text.count("read_only: true"), 5)
        self.assertEqual(text.count("no-new-privileges:true"), 2)
        self.assertEqual(text.count("- ALL"), 2)
        self.assertNotIn("privileged:", text)
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertNotIn("network_mode: host", text)
        self.assertIn('test: ["CMD", "rpi-streamer", "healthcheck"]', text)
        self.assertIn("condition: service_healthy", text)

    def test_images_run_as_non_root_and_have_provenance_labels(self) -> None:
        indexer = INDEXER.read_text(encoding="utf-8")
        nginx = NGINX.read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", indexer)
        self.assertIn("USER 101:10001", nginx)
        for text in (indexer, nginx):
            self.assertIn("@sha256:", text)
            self.assertIn("org.opencontainers.image.version", text)
            self.assertIn("org.opencontainers.image.revision", text)
            self.assertIn("org.opencontainers.image.created", text)
            self.assertNotIn(":latest", text)

    def test_container_paths_and_multi_platform_metadata_agree(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        site = SITE.read_text(encoding="utf-8")
        bake = BAKE.read_text(encoding="utf-8")
        self.assertIn("RPI_STREAMER_SITE_DIR: /state/site", compose)
        self.assertIn("root /state/site/;", site)
        self.assertIn("alias /media/$media_file;", site)
        self.assertIn('platforms = ["linux/amd64", "linux/arm64"]', bake)

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is not installed")
    def test_compose_model_is_valid(self) -> None:
        completed = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
