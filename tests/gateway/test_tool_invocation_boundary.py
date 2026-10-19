from __future__ import annotations

import json

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools.invocation import REMOTE_TOOLS, ToolCallPreparationError, prepare_tool_call


def _prepare(params: dict[str, object]):
    return prepare_tool_call(params, broker_spec=mcp_server._TOOL_BROKER_SPEC, remote_tools=mcp_server._REMOTE_TOOLS)


def test_tool_call_normalization_contract() -> None:
    run = _prepare({"name": "run", "arguments": json.dumps({"command": "printf ok"})})
    assert run.name == "bash" and run.args == {"command": "printf ok"} and run.spec is mcp_server.TOOLS["bash"]
    assert _prepare({"name": "read", "arguments": ["not", "an", "object"]}).args == {}


def test_tool_call_error_contract() -> None:
    with pytest.raises(ToolCallPreparationError) as unknown:
        _prepare({"name": "definitely-not-a-tool", "arguments": {}})
    assert unknown.value.code == -32601 and str(unknown.value) == "unknown tool: definitely-not-a-tool"
    with pytest.raises(ToolCallPreparationError) as bad_args:
        _prepare({"name": "memory", "arguments": {"op": "recall", "definitely_unknown": True}})
    assert bad_args.value.code == -32602 and "definitely_unknown" in str(bad_args.value)


def test_tool_call_routing_contract() -> None:
    assert _prepare({"name": "context", "arguments": {"mode": "symbols"}}).remote_routed is False
    assert _prepare({"name": "context", "arguments": {}}).remote_routed is True
    assert mcp_server._REMOTE_TOOLS is REMOTE_TOOLS
