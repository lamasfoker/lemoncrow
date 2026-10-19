from __future__ import annotations

from lemoncrow.gateway.adapters.mcp.jsonrpc import error, ok
from lemoncrow.gateway.tools.results import clean_tool_result


def test_tool_call_paths_use_the_canonical_result_normalization() -> None:
    # Normalization moved out of mcp_server into the extracted tool lifecycle
    # and prepared-execution runtime; neither may carry its own copy.
    from lemoncrow.gateway.tools import execution, lifecycle

    assert lifecycle.clean_tool_result is clean_tool_result
    assert execution.clean_tool_result is clean_tool_result


def test_result_normalization_preserves_semantic_empty_values() -> None:
    result = clean_tool_result(
        {
            "none": None,
            "empty": "",
            "zero": 0,
            "false": False,
            "list": [],
            "dict": {},
            "nested": {"drop": "", "keep": "value"},
        },
        "read",
    )
    assert result == {
        "zero": 0,
        "false": False,
        "list": [],
        "dict": {},
        "nested": {"keep": "value"},
    }


def test_jsonrpc_envelopes_are_canonical() -> None:
    assert ok(7, {"value": 1}) == {"jsonrpc": "2.0", "id": 7, "result": {"value": 1}}
    assert error(7, -32601, "missing") == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "missing"},
    }
