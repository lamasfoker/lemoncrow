from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SERVER = REPO_ROOT / "scripts" / "local_server.sh"


def test_prepare_rejects_missing_transitive_runtime_dependency(tmp_path: Path) -> None:
    """A package-root import must not let a broken release reach the supervisor."""
    home = tmp_path / "home"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == '-c' && \"${2:-}\" == *'lemoncrow_server_core.bootstrap'* ]]; then\n"
        "  echo \"ModuleNotFoundError: No module named 'fastapi'\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_FRONTEND_DIR": str(tmp_path / "frontend"),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
        }
    )

    result = subprocess.run(
        ["bash", str(LOCAL_SERVER), "_prepare"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError: No module named 'fastapi'" in result.stderr
    assert "local server dependencies are not importable in the selected product engine" in result.stderr
    assert not (home / ".lemoncrow" / "env").exists()
