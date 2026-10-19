"""Shared contract tests for the Cursor and Hermes Agent adapters."""

from __future__ import annotations

from unittest.mock import MagicMock

from lemoncrow.gateway.adapters.cursor_adapter import CursorAdapter, CursorConfig
from lemoncrow.gateway.adapters.hermes_adapter import HermesAdapter, HermesConfig
from lemoncrow.gateway.sdk import LemonCrowClient
from lemoncrow.gateway.sdk.client import ContextResult


def _client() -> MagicMock:
    client = MagicMock(spec=LemonCrowClient)
    client.get_context.return_value = ContextResult(context="test reasoning context")
    return client


def test_adapter_configuration_contract() -> None:
    cases = [
        (CursorAdapter, CursorConfig, "cursor", "Agent.cursor", ["context", "trace"], "enforce"),
        (HermesAdapter, HermesConfig, "hermes", "Agent.hermes", ["context", "trace", "rescue"], "suggest"),
    ]
    for adapter_type, config_type, host, domain, tools, mode in cases:
        default = adapter_type.from_config(config_type(), client=_client())
        assert (default.host, default.mode, default.default_domain, default.default_tools) == (host, "shadow", None, [])
        custom = adapter_type.from_config(
            config_type(mode=mode, default_domain=domain, default_tools=tools), client=_client()
        )
        assert (custom.host, custom.mode, custom.default_domain, custom.default_tools) == (host, mode, domain, tools)


def test_adapter_context_delegation_contract() -> None:
    cases = [
        (
            CursorAdapter,
            CursorConfig(mode="suggest"),
            {"task": "Add logging", "domain": "python", "files": ["auth.py"], "tools": ["read", "edit"]},
        ),
        (
            HermesAdapter,
            HermesConfig(mode="enforce"),
            {"task": "Deploy", "domain": "infra", "files": None, "tools": ["shell"]},
        ),
    ]
    for adapter_type, config, kwargs in cases:
        client = _client()
        adapter = adapter_type.from_config(config, client=client)
        prime = adapter.prime_context(**kwargs)
        assert isinstance(prime, ContextResult) and "test reasoning context" in prime.context
        client.get_context.assert_called_once_with(**kwargs)
        client.reset_mock()
        context = adapter.get_context(**kwargs)
        assert isinstance(context, ContextResult) and "test reasoning context" in context.context


def test_adapter_decision_contract() -> None:
    cases = [
        (CursorAdapter, CursorConfig(mode="suggest"), "cursor", "suggest"),
        (CursorAdapter, CursorConfig(mode="enforce"), "cursor", "enforce"),
        (HermesAdapter, HermesConfig(mode="enforce"), "hermes", "enforce"),
        (HermesAdapter, HermesConfig(mode="shadow"), "hermes", "shadow"),
    ]
    for adapter_type, config, host, mode in cases:
        adapter = adapter_type.from_config(config, client=_client())
        decision = adapter.get_decision(task="task")
        assert (decision.host, decision.mode, decision.blocked) == (host, mode, False)
        assert "test reasoning context" in decision.reasoning_context


def test_adapter_installation_instructions_contract() -> None:
    for adapter_type, host_name, marker in [
        (CursorAdapter, "Cursor", ".cursor/mcp.json"),
        (HermesAdapter, "Hermes", "config.yaml"),
    ]:
        instructions = adapter_type.install()
        assert isinstance(instructions, str) and host_name in instructions and "lc" in instructions
        if adapter_type is HermesAdapter:
            assert marker in instructions or "HERMES_HOME" in instructions
        else:
            assert marker in instructions
