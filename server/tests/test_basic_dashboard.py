"""The local Dashboard is the basic, read-only subset of the dashboard API.

Sessions, usage, savings, settings and system pages are served on loopback,
with the deterministic code map over the Runtime Lite index. Code ask/editor,
references, module purposes, agent handoff, Review capture from Code,
knowledge, swarms, workflow, memory and rubrics are hosted-only, and nothing
but the telemetry consent the Dashboard itself asks for is written through it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from lemoncrow_server_core.dashboard_service import BasicDashboardApp, basic_dashboard_allows


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("HEAD", "/ready"),
        ("GET", "/config"),
        ("GET", "/overview"),
        ("GET", "/traces"),
        ("GET", "/v1/traces/lt_1"),
        ("GET", "/v1/sessions"),
        ("GET", "/v1/sessions/s-1"),
        ("GET", "/ledgers/s-1"),
        ("GET", "/v1/outcomes/summary"),
        ("GET", "/v1/insights"),
        ("GET", "/raw-artifacts/a-1/content"),
        ("GET", "/v1/files/content"),
        ("GET", "/analytics/dashboard"),
        ("GET", "/calls"),
        ("GET", "/pricing"),
        ("GET", "/v1/reports"),
        ("GET", "/savings"),
        ("GET", "/v1/savings/summary"),
        ("GET", "/v1/optimizations/summary"),
        ("GET", "/telemetry/summary"),
        ("GET", "/watchdogs/config"),
        ("GET", "/hosts"),
        ("GET", "/mcp/status"),
        ("GET", "/skills/review"),
        ("GET", "/agents"),
        ("POST", "/telemetry/config"),
        ("POST", "/telemetry/ack"),
        ("GET", "/v1/code-map/projects"),
        ("GET", "/v1/code-map/modules"),
        ("GET", "/v1/code-map/relationships"),
        ("GET", "/v1/code-map/focus"),
        ("GET", "/v1/code-map/files"),
        ("GET", "/v1/code-map/file-symbols"),
        ("GET", "/v1/code-map/file-content"),
        ("HEAD", "/v1/code-map/file-content"),
        ("GET", "/v1/code-map/search"),
        ("GET", "/v1/code-map/symbol"),
        ("GET", "/v1/code-map/changes"),
        ("GET", "/v1/code-map/file-changes"),
        ("GET", "/v1/code-map/hotspots"),
        ("GET", "/v1/code-map/activity"),
        ("GET", "/v1/code-map/editor"),
        ("GET", "/v1/code-map/ask/options"),
        ("POST", "/v1/code-map/editor"),
        ("POST", "/v1/code-map/ask/stream"),
        ("POST", "/v1/code-map/review"),
    ],
)
def test_basic_dashboard_serves_sessions_usage_savings_settings_and_system(method: str, path: str) -> None:
    assert basic_dashboard_allows(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/code-map/references"),
        ("POST", "/v1/code-map/module-purpose"),
        ("POST", "/v1/code-map/agent-handoff"),
        ("POST", "/v1/code-map/files"),
        ("GET", "/v1/code-map/full"),
        ("GET", "/v1/code-map/files/extra"),
        ("GET", "/v1/code-map/../memory/blocks"),
        ("GET", "/v1/swarm/runs"),
        ("POST", "/v1/swarm/runs"),
        ("GET", "/v1/workflow/current"),
        ("GET", "/v1/swarm/runs"),
        ("POST", "/v1/swarm/runs"),
        ("GET", "/v1/workflow/current"),
        ("GET", "/v1/memory/blocks"),
        ("POST", "/v1/memory/recall"),
        ("GET", "/v1/rubrics"),
        ("POST", "/v1/rubrics/run"),
        ("POST", "/v1/reasoning/rescue"),
        ("GET", "/blocks"),
        ("GET", "/clusters"),
        ("GET", "/plans"),
        ("POST", "/v1/traces"),
        ("POST", "/feedback"),
        ("POST", "/watchdogs/config"),
        ("POST", "/telemetry/local"),
        ("DELETE", "/v1/sessions/s-1"),
        ("GET", "/configuration"),
        ("GET", "/traces/../v1/memory/blocks"),
        ("GET", "/traces/./../blocks"),
    ],
)
def test_basic_dashboard_refuses_hosted_only_pages_and_writes(method: str, path: str) -> None:
    assert not basic_dashboard_allows(method, path)


class _Inner:
    def __init__(self) -> None:
        self.state = type("State", (), {})()
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.seen.append((scope["type"], scope.get("path", "")))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})


async def _call(app: Any, scope: dict[str, Any]) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next((int(m["status"]) for m in sent if m["type"] == "http.response.start"), 0)
    body = b"".join(bytes(m.get("body") or b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def test_basic_dashboard_app_answers_unavailable_pages_without_reaching_the_full_api() -> None:
    inner = _Inner()
    app = BasicDashboardApp(inner)
    status, body = asyncio.run(_call(app, {"type": "http", "method": "GET", "path": "/v1/code-map/references"}))
    assert status == 404
    assert "hosted" in json.loads(body)["detail"]
    assert inner.seen == []

    assert asyncio.run(_call(app, {"type": "http", "method": "GET", "path": "/traces"})) == (200, b"inner")
    assert inner.seen == [("http", "/traces")]
    # The wrapped app's state stays reachable (the bridge sets code intelligence on it).
    assert app.state is inner.state


def test_the_local_dashboard_app_is_the_basic_subset() -> None:
    pytest.importorskip("lemoncrow.core.service.api", reason="the dashboard data still comes from the full package")
    from lemoncrow_server_core.dashboard_service import default_dashboard_app

    app = default_dashboard_app(None)
    assert isinstance(app, BasicDashboardApp)
    assert app.state.code_intelligence_for_root is None
