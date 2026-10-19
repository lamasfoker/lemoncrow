from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_orchestration_hooks_late_bind_agent_runtime(monkeypatch) -> None:
    def fake_select(*_args, **_kwargs):
        return object()

    def fake_execute(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(mcp_server, "select_owned_route", fake_select)
    monkeypatch.setattr(mcp_server, "execute_owned_prompt", fake_execute)
    hooks = mcp_server._orchestration_handler_hooks()
    assert hooks.select_owned_route is fake_select
    assert hooks.execute_owned_prompt is fake_execute


def test_orchestration_hooks_late_bind_workflow_runtime(monkeypatch) -> None:
    def fake_run(args):
        return {"seen": args}

    monkeypatch.setattr(mcp_server, "_run_owned_workflow", fake_run)
    hooks = mcp_server._orchestration_handler_hooks()
    assert hooks.run_owned_workflow is fake_run
    assert hooks.run_owned_workflow({"x": 1}) == {"seen": {"x": 1}}
