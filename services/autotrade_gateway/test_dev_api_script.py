"""Regression tests for the simulation-only sidecar launcher."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
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
