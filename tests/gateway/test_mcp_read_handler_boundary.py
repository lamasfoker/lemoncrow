from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_read_hooks_late_bind_mcp_helpers(monkeypatch) -> None:
    marker = object()

    def fake_reader(*_args, **_kwargs):
        return marker

    monkeypatch.setattr(mcp_server, "_smart_read_single", fake_reader)
    hooks = mcp_server._read_handler_hooks()
    assert hooks.smart_read_single is fake_reader
    assert hooks.smart_read_single() is marker
