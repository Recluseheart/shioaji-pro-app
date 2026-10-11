"""Regression tests for the simulation-only sidecar launcher."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DevApiScriptTest(unittest.TestCase):
    def run_launcher(self, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "scripts/dev-api.sh"],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], **environment},
            capture_output=True,
            check=False,
            text=True,
        )

    def test_refuses_production_before_resolving_or_starting_a_sidecar(self) -> None:
        completed = self.run_launcher(SJ_PRODUCTION="true")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with SJ_PRODUCTION enabled", completed.stderr)

    def test_refuses_ca_configuration_before_resolving_or_starting_a_sidecar(self) -> None:
        completed = self.run_launcher(SJ_CA_PATH="/private/ca.pem")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with CA configuration", completed.stderr)

    def test_refuses_tls_transport_before_resolving_or_starting_a_sidecar(self) -> None:
        completed = self.run_launcher(SJ_HTTP_TLS_CERT="/private/sidecar.pem")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with TLS or ACME transport configuration", completed.stderr)

    def test_refuses_http3_transport_before_resolving_or_starting_a_sidecar(self) -> None:
        completed = self.run_launcher(SJ_HTTP3="true")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with HTTP/3 transport configuration", completed.stderr)

    def test_refuses_production_configuration_in_an_untracked_dotenv(self) -> None:
        completed = self.run_dotenv_launcher("SJ_PRODUCTION=TRUE\n")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with production configuration in .env", completed.stderr)

    def test_refuses_ca_configuration_in_an_untracked_dotenv(self) -> None:
        completed = self.run_dotenv_launcher("SJ_CA_PATH=/private/ca.pem\n")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with CA configuration in .env", completed.stderr)

    def test_refuses_tls_transport_configuration_in_an_untracked_dotenv(self) -> None:
        completed = self.run_dotenv_launcher("SJ_HTTP_ACME_CACHE=/private/acme\n")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with TLS or ACME transport configuration in .env", completed.stderr)

    def test_refuses_http3_transport_configuration_in_an_untracked_dotenv(self) -> None:
        completed = self.run_dotenv_launcher("SJ_HTTP3=TRUE\n")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing to start dev api with HTTP/3 transport configuration in .env", completed.stderr)

    def run_dotenv_launcher(self, dotenv: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy(ROOT / "scripts" / "dev-api.sh", scripts / "dev-api.sh")
            (root / ".env").write_text(dotenv, encoding="utf-8")
            return subprocess.run(
                ["sh", "scripts/dev-api.sh"],
                cwd=root,
                env={"PATH": os.environ["PATH"]},
                capture_output=True,
                check=False,
                text=True,
            )
