"""``lc runtime mcp serve|client|service|gateway`` in a full install reaches the client.

The remote MCP gateway, tunnel and units live in ``lemoncrow_client.remote``;
the full CLI keeps only a passthrough so ``lc`` behaves the same in every install.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TextIO

import pytest
from click.testing import CliRunner
from lemoncrow_client.remote import cli as remote_cli

from lemoncrow.gateway.cli.commands.mcp import mcp_group


def test_remote_commands_forward_verbatim_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    for _case_index, arguments in enumerate(
        [
            ["serve", "--persistent", "--hostname", "mcp.example.com"],
            ["serve", "--help"],
            ["client", "--redirect-uri", "https://claude.ai/api/mcp/auth_callback"],
            ["service", "restart", "mcp.example.com", "--new-pairing-code"],
            ["gateway", "--port", "7421"],
        ]
    ):
        with monkeypatch.context() as case_monkeypatch:
            seen: list[Any] = []

            def fake(forwarded: Sequence[str], sink: TextIO, _seen: list[Any] = seen, **_: Any) -> int:
                _seen.append(list(forwarded))
                return 3

            case_monkeypatch.setattr(remote_cli, "remote_main", fake)
            result = CliRunner().invoke(mcp_group, arguments)
            assert seen == [arguments]
            assert result.exit_code == 3
