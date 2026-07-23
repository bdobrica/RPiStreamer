from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
UNIT = ROOT / "deployment" / "systemd" / "rpi-streamer.service"
INSTALLER = ROOT / "deployment" / "install.sh"


class NativeDeploymentTests(unittest.TestCase):
    def test_systemd_unit_runs_unprivileged_and_is_hardened(self) -> None:
        text = UNIT.read_text(encoding="utf-8")

        required = (
            "User=rpi-streamer",
            "Group=rpi-streamer",
            "ExecReload=/bin/kill -HUP $MAINPID",
            "Restart=on-failure",
            "StateDirectory=rpi-streamer",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ReadWritePaths=/var/lib/rpi-streamer",
            "UMask=0027",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, text)
        self.assertNotIn("User=root", text)
        self.assertNotIn("ReadWritePaths=/mnt/anime", text)

    def test_account_state_and_config_assets_are_consistent(self) -> None:
        sysusers = (ROOT / "deployment" / "sysusers" / "rpi-streamer.conf").read_text(
            encoding="utf-8"
        )
        tmpfiles = (ROOT / "deployment" / "tmpfiles" / "rpi-streamer.conf").read_text(
            encoding="utf-8"
        )
        config = (ROOT / "deployment" / "config" / "rpi-streamer.ini").read_text(
            encoding="utf-8"
        )

        self.assertIn("rpi-streamer", sysusers)
        self.assertIn("/usr/sbin/nologin", sysusers)
        self.assertIn("/var/lib/rpi-streamer", tmpfiles)
        self.assertIn("0750 rpi-streamer rpi-streamer", tmpfiles)
        self.assertIn("media_root = /mnt/anime", config)
        self.assertIn("state_dir = /var/lib/rpi-streamer", config)

    def test_installer_has_valid_shell_syntax_and_preserves_config(self) -> None:
        subprocess.run(
            ["sh", "-n", str(INSTALLER)],
            check=True,
            capture_output=True,
            text=True,
        )
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("pip install --upgrade", text)
        self.assertIn("if [ ! -e /etc/rpi-streamer/rpi-streamer.ini ]", text)
        self.assertIn("nginx -t", text)
        self.assertIn("systemctl daemon-reload", text)

    @unittest.skipUnless(
        shutil.which("systemd-analyze"), "systemd-analyze is not installed"
    )
    def test_systemd_unit_syntax(self) -> None:
        completed = subprocess.run(
            ["systemd-analyze", "verify", str(UNIT)],
            check=False,
            capture_output=True,
            text=True,
        )
        if "SO_PASSCRED failed: Operation not permitted" in completed.stderr:
            self.skipTest(
                "systemd-analyze cannot open its sandbox communication socket"
            )
        diagnostics = completed.stderr.replace(
            "/opt/rpi-streamer/venv/bin/rpi-streamer", "<installed executable>"
        )
        diagnostics = "\n".join(
            line for line in diagnostics.splitlines() if "not executable" not in line
        )
        self.assertEqual(completed.returncode, 0, diagnostics)
