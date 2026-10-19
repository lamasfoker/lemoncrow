from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from lemoncrow_client import pi_host
from lemoncrow_client.cli import main


def _archive_bytes() -> bytes:
    payload = b"""#!/bin/sh
if [ "${1:-}" = "--help" ]; then
  cat <<'HELP'
--offline --no-tools --no-context-files --no-extensions --no-skills
--no-prompt-templates --no-approve --session-dir --provider --model --extension
HELP
  exit 0
fi
exit 0
"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("pi")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        archive.addfile(info)
        executable = tarfile.TarInfo("pi/pi")
        executable.mode = 0o755
        executable.size = len(payload)
        archive.addfile(executable, io.BytesIO(payload))
    return buffer.getvalue()


def test_thin_pi_install_status_remove_is_self_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive_bytes()
    checksum = hashlib.sha256(archive).hexdigest()
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(pi_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pi_host.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(pi_host._ASSET_SHA256, ("linux", "x64"), checksum)
    monkeypatch.setattr(pi_host, "_download_bytes", lambda _url: archive)

    import io as _io

    sink = _io.StringIO()
    assert main(["rt", "host", "install", "--engine", "pi"], sink) == 0
    assert "Pi host installed" in sink.getvalue()
    assert (tmp_path / "pi" / "lemoncrow" / "lite.mjs").is_file()

    status = _io.StringIO()
    assert main(["rt", "host", "status", "--engine", "pi", "--json"], status) == 0
    assert '"installed": true' in status.getvalue()

    removed = _io.StringIO()
    assert main(["rt", "host", "remove", "--engine", "pi", "--yes"], removed) == 0
    assert "Pi host removed" in removed.getvalue()
    assert not (tmp_path / "pi" / "lemoncrow" / "lite.mjs").exists()


def test_thin_pi_status_needs_no_full_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io as _io

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("lemoncrow_client.cli._full_runtime_executable", lambda: None)
    sink = _io.StringIO()
    assert main(["rt", "host", "status", "--engine", "pi", "--json"], sink) == 0
    assert '"installed": false' in sink.getvalue()
