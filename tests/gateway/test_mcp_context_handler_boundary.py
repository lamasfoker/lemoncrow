from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_context_hooks_late_bind_runtime(monkeypatch) -> None:
    marker = object()

    def fake_runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", fake_runtime)
    hooks = mcp_server._context_handler_hooks()
    assert hooks.runtime is fake_runtime
    assert hooks.runtime() is marker
