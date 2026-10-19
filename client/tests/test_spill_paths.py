from __future__ import annotations

import os
from pathlib import Path

import pytest
from lemoncrow_client.kit import spill


def test_explicit_spill_dir_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(configured))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert spill.resolve_spill_dir() == configured.resolve()


def test_xdg_runtime_dir_is_preferred_over_system_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(spill.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))

    resolved = spill.resolve_spill_dir()

    assert resolved == (runtime / "lemoncrow" / "spill").resolve()
    if os.name != "nt":
        assert resolved.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0, reason="needs a directory this user cannot write")
def test_an_unwritable_runtime_dir_falls_back_to_the_user_temp_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "someone-elses-runtime"
    runtime.mkdir(mode=0o500)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(spill.tempfile, "gettempdir", lambda: str(temp_root))
    try:
        resolved = spill.resolve_spill_dir()
    finally:
        runtime.chmod(0o700)

    assert resolved == (temp_root / f"lemoncrow-{os.getuid()}" / "spill").resolve()


def test_system_temp_fallback_is_user_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(spill.tempfile, "gettempdir", lambda: str(temp_root))

    resolved = spill.resolve_spill_dir()

    if hasattr(os, "getuid"):
        expected = temp_root / f"lemoncrow-{os.getuid()}" / "spill"
    else:
        expected = temp_root / "lemoncrow" / "spill"
    assert resolved == expected.resolve()
    assert resolved.is_dir()
    if os.name != "nt":
        assert resolved.stat().st_mode & 0o777 == 0o700
