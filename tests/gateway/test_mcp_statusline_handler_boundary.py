from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_statusline_handler_hooks_are_late_bound(tmp_path, monkeypatch) -> None:
    root = tmp_path / ".lemoncrow"
    sidecar = root / "sessions" / "session-1" / "savings.jsonl"
    monkeypatch.setattr(mcp_server, "_lemoncrow_root", lambda: root)
    monkeypatch.setattr(mcp_server, "_get_host_session_sidecar_path", lambda: sidecar)
    hooks = mcp_server._statusline_handler_hooks()
    assert hooks.lemoncrow_root() == root
    assert hooks.session_sidecar() == sidecar
