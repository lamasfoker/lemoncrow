from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "integrations" / "claude" / "plugin" / "tests" / "verified-work-node-harness.mjs"


def test_verified_work_mod_native_bash_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the Claude Mod contract harness")
    result = subprocess.run(
        [node, str(HARNESS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "verified-work Mod node harness: ok" in result.stdout
