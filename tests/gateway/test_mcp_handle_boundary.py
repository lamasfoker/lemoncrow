from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import call_runtime, runtime_call


def test_tool_call_lifecycle_owns_finalization() -> None:
    call_source = inspect.getsource(runtime_call.ToolCallRuntime.run)
    lifecycle_source = inspect.getsource(mcp_server._ToolCallLifecycle)
    assert "ToolCallLifecycle(" in call_source
    assert "def finalize_payload" in lifecycle_source
    assert "def finalize_error_payload" in lifecycle_source
    assert "def record_error" in lifecycle_source
    assert "def finalize_response" not in lifecycle_source
    assert "def finalize_error_response" not in lifecycle_source
    assert len(call_source.splitlines()) <= 120


def test_tool_call_lifecycle_exposes_transport_neutral_payload() -> None:
    source = inspect.getsource(mcp_server._ToolCallLifecycle)
    assert "def finalize_payload" in source
    assert "return response_payload" in source
    assert "_ok(" not in source
    mcp_wrapper = inspect.getsource(mcp_server._finalize_mcp_response)
    assert "_ok(lifecycle.rid, lifecycle.finalize_payload(result))" in mcp_wrapper


def test_mcp_and_payload_wrappers_share_the_same_runtime() -> None:
    mcp_source = inspect.getsource(mcp_server._handle_tool_call)
    payload_source = inspect.getsource(mcp_server._handle_tool_payload)
    assert "_run_tool_call(rid, params, payload_only=False)" in mcp_source
    assert "execute_default_tool_payload(rid, params)" in payload_source
    assert call_runtime.default_tool_runtime_configured() is True
