from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_review_hook_factory_late_binds_legacy_monkeypatches(monkeypatch) -> None:
    def session() -> tuple[str, str]:
        return "sid-x", "claude"

    def model() -> str:
        return "model-x"

    monkeypatch.setattr(mcp_server, "_resolved_host_session", session)
    monkeypatch.setattr(mcp_server, "_get_mcp_model", model)
    hooks = mcp_server._review_handler_hooks()
    assert hooks.resolved_host_session is session
    assert hooks.get_mcp_model is model
