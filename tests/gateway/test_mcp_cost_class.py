"""Contract tests for per-request MCP cost-class routing."""

from __future__ import annotations

from typing import Any

from lemoncrow.gateway.adapters.mcp_server import _COST_CPU, _COST_DETACHED, _COST_IO, _classify_cost, _is_heavy_request


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"method": "tools/call", "params": params}


def test_cpu_lane_contract() -> None:
    for request in [
        {"method": "tools/list"},
        {"method": "initialize"},
        {"method": "notifications/initialized"},
        {"method": "ping"},
        {},
        _call("read", {"foo": "bar"}),
        _call("grep", {"foo": "bar"}),
        _call("code_search", {"foo": "bar"}),
        _call("smart_read", {"foo": "bar"}),
        _call("trace", {"foo": "bar"}),
        _call("get_context"),
        _call("memory", {"op": "recall", "query": "x"}),
        _call("memory"),
        _call("totally_made_up_tool"),
        {"method": "tools/call", "params": "nope"},
    ]:
        assert _classify_cost(request) == _COST_CPU, request


def test_bash_lane_contract() -> None:
    cases = [
        (_call("bash", {"command": "ls -la"}), _COST_IO),
        (_call("bash"), _COST_IO),
        (_call("bash", {"command": "sleep 100", "background": True}), _COST_DETACHED),
        (_call("bash", {"command": "sleep 100 &"}), _COST_DETACHED),
        (_call("bash", {"command": "sleep 100 &   "}), _COST_DETACHED),
        (_call("bash", {"command": "make lint && make test"}), _COST_IO),
        (_call("run", {"command": "echo hi"}), _COST_IO),
        (_call("run", {"command": "echo hi &"}), _COST_DETACHED),
        ({"method": "tools/call", "params": {"name": "bash", "arguments": "nope"}}, _COST_IO),
    ]
    for request, expected in cases:
        assert _classify_cost(request) == expected, request


def test_non_cpu_tool_contract() -> None:
    cases = [
        (_call("edit", {"x": 1}), _COST_IO),
        (_call("web_fetch", {"x": 1}), _COST_IO),
        (_call("memory", {"op": "store_fact", "text": "x"}), _COST_IO),
        (_call("workflow", {"task": "do it"}), _COST_DETACHED),
        (_call("agent", {"task": "do it"}), _COST_DETACHED),
    ]
    for request, expected in cases:
        assert _classify_cost(request) == expected, request


def test_heavy_request_compatibility_contract() -> None:
    for request, expected in [
        (_call("bash", {"command": "ls"}), True),
        (_call("edit"), True),
        (_call("workflow", {"task": "x"}), True),
        (_call("read", {"path": "a"}), False),
        ({"method": "tools/list"}, False),
    ]:
        assert _is_heavy_request(request) is expected, request
