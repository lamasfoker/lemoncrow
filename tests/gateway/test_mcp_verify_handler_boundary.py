from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_verify_hook_factory_late_binds_runtime(monkeypatch) -> None:
    marker = object()

    def runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", runtime)
    hooks = mcp_server._verify_handler_hooks()
    assert hooks.runtime is runtime
    assert hooks.runtime() is marker
