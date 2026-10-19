from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_rescue_hooks_are_late_bound(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(mcp_server, "_runtime", lambda: marker)
    hooks = mcp_server._rescue_handler_hooks()
    assert hooks.runtime() is marker
