from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import state


def test_request_code_engine_override_round_trips_nested_values() -> None:
    first = object()
    second = object()

    prior = state.set_request_code_context_engine(first)
    assert prior is state.NO_CODE_ENGINE_OVERRIDE
    assert state.request_code_engine_override.value is first

    nested_prior = state.set_request_code_context_engine(second)
    assert nested_prior is first
    assert state.request_code_engine_override.value is second

    state.clear_request_code_context_engine(nested_prior)
    assert state.request_code_engine_override.value is first
    state.clear_request_code_context_engine(prior)
    assert not hasattr(state.request_code_engine_override, "value")


def test_mcp_server_reexports_canonical_code_engine_state() -> None:
    assert mcp_server._request_code_engine_override is state.request_code_engine_override
    assert mcp_server._NO_CODE_ENGINE_OVERRIDE is state.NO_CODE_ENGINE_OVERRIDE
    assert mcp_server._set_request_code_context_engine is state.set_request_code_context_engine
    assert mcp_server._clear_request_code_context_engine is state.clear_request_code_context_engine
