"""Shell-profile persistence stays PATH-only now that lcr is executable.

Older installs wrote an lcr alias inside the LemonCrow-owned sentinel block.
Re-running the installer rewrites that block, removing the legacy alias without
touching unrelated user lines.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "lib" / "common.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"

_HARNESS = """
set -u
C_FRAME=""; C_RESET=""; C_GREEN=""
LEMONCROW_BIN_DIR="$HOME/.lemoncrow/bin"
LEMONCROW_NODE_DIR="$HOME/.lemoncrow/node"
source {common} >/dev/null 2>&1 || true
_ensure_path_persistence >/dev/null 2>&1
"""


def _persist(home: Path, *, shell: str, times: int = 1) -> str:
    env = os.environ.copy()
    env.update({"HOME": str(home), "SHELL": shell})
    script = _HARNESS.format(common=COMMON) * times
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True, env=env)
    profile = {
        "/bin/bash": home / ".bashrc",
        "/usr/bin/zsh": home / ".zshrc",
        "/usr/bin/fish": home / ".config" / "fish" / "config.fish",
    }[shell]
    return profile.read_text() if profile.exists() else ""


def test_installer_writes_path_without_review_alias(tmp_path: Path) -> None:
    profile = _persist(tmp_path, shell="/bin/bash")
    assert 'export PATH="' in profile
    assert "alias lcr" not in profile


def test_reinstall_is_idempotent_without_alias(tmp_path: Path) -> None:
    profile = _persist(tmp_path, shell="/bin/bash", times=3)
    assert profile.count("# >>> lemoncrow path setup >>>") == 1
    assert profile.count("alias lcr") == 0


def test_fish_profile_is_path_only(tmp_path: Path) -> None:
    profile = _persist(tmp_path, shell="/usr/bin/fish")
    assert "alias lcr" not in profile
    assert 'export PATH="' in profile


def test_reinstall_removes_legacy_alias_from_owned_block(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text(
        "echo before\n"
        "# >>> lemoncrow path setup >>>\n"
        'export PATH="$HOME/.lemoncrow/bin:$PATH"\n'
        "alias lcr='lc review'\n"
        "# <<< lemoncrow path setup <<<\n"
        "echo after\n"
    )

    updated = _persist(tmp_path, shell="/bin/bash")

    assert "alias lcr" not in updated
    assert "echo before" in updated
    assert "echo after" in updated


def test_installer_reports_exact_path_directories(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow" / "node" / "bin").mkdir(parents=True)
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path), "SHELL": "/bin/bash"})

    result = subprocess.run(
        [
            "bash",
            "-c",
            _HARNESS.format(common=COMMON).replace(
                "_ensure_path_persistence >/dev/null 2>&1", "_ensure_path_persistence"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "PATH configured in ~/.bashrc (~/.lemoncrow/bin)" in result.stdout


def test_uninstall_takes_owned_path_block_away(tmp_path: Path) -> None:
    home = tmp_path
    _persist(home, shell="/bin/bash")
    profile = home / ".bashrc"
    profile.write_text("echo before\n" + profile.read_text() + "echo after\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    service_calls = tmp_path / "service-calls.log"
    for name in ("lc", "systemctl"):
        shim = fake_bin / name
        shim.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}' >> '{service_calls}'\nexit 1\n")
        shim.chmod(0o755)

    env = {
        "HOME": str(home),
        "SHELL": "/bin/bash",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "LEMONCROW_BIN_DIR": str(home / ".lemoncrow" / "bin"),
        "LEMONCROW_TOOL_DIR": str(home / ".lemoncrow" / "uv-tools"),
    }
    subprocess.run(
        ["bash", str(UNINSTALL), "--profile-only"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert not service_calls.exists()

    remaining = profile.read_text()
    assert "lemoncrow" not in remaining
    assert "echo before" in remaining
    assert "echo after" in remaining
