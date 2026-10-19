from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client import host_trial, pi_integration


def test_install_reinstall_remove_preserves_native_pi_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(host_trial, "_runtime_mcp_invocation", lambda _host: ("/opt/bin/lc", ["runtime", "lite-mcp"]))
    directory = tmp_path / ".pi" / "agent"
    directory.mkdir(parents=True)
    settings = directory / "settings.json"
    original = {"defaultModel": "custom", "theme": "dark", "extensions": ["other.ts", {"path": "package"}]}
    settings.write_text(json.dumps(original))
    auth = directory / "auth.json"
    auth.write_text('{"provider":{"token":"unchanged"}}')
    pi_integration.install()
    pi_integration.install()
    extension = directory / "lemoncrow" / "lite.mjs"
    configured = json.loads(settings.read_text())
    assert configured == {**original, "extensions": [*original["extensions"], str(extension)]}
    assert pi_integration.enabled()
    assert '"/opt/bin/lc"' in extension.read_text()
    assert pi_integration.remove()
    assert not pi_integration.remove()
    assert json.loads(settings.read_text()) == original
    assert auth.read_text() == '{"provider":{"token":"unchanged"}}'
    assert not extension.exists()


@pytest.mark.parametrize("content", ["{broken", "[]", '{"extensions":"bad"}'])
def test_invalid_settings_fail_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    settings = tmp_path / "settings.json"
    settings.write_text(content)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        pi_integration.install()
    assert settings.read_text() == content
    assert not (tmp_path / "lemoncrow").exists()


def test_shell_uninstaller_dry_run_and_removal_preserve_other_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    settings = tmp_path / "settings.json"
    settings.write_text('{"extensions":["other.ts"],"defaultModel":"custom"}')
    pi_integration.install()
    before = settings.read_text()
    script = Path(__file__).resolve().parents[2] / "scripts" / "uninstall_pi.sh"
    subprocess.run(["bash", str(script), "--dry-run"], check=True, capture_output=True)
    assert settings.read_text() == before
    subprocess.run(["bash", str(script)], check=True, capture_output=True)
    assert json.loads(settings.read_text()) == {"extensions": ["other.ts"], "defaultModel": "custom"}
    assert not (tmp_path / "lemoncrow" / "lite.mjs").exists()
