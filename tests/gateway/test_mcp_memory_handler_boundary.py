from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_memory_hook_factory_late_binds_legacy_monkeypatches(monkeypatch) -> None:
    marker = object()

    def symbol_recall():
        return marker

    monkeypatch.setattr(mcp_server, "_symbol_recall", symbol_recall)
    hooks = mcp_server._memory_handler_hooks()
    assert hooks.symbol_recall is symbol_recall
    assert hooks.symbol_recall() is marker
