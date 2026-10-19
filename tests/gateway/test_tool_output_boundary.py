from __future__ import annotations

from lemoncrow_client.kit.read_path import split_read_range_suffix

from lemoncrow.gateway.adapters import mcp_server


def test_read_range_parser_has_one_canonical_implementation() -> None:
    assert mcp_server._split_read_range_suffix is split_read_range_suffix
    assert split_read_range_suffix("src/app.py:L10-L20") == ("src/app.py", "L10-L20")
    assert split_read_range_suffix("src/app.py:summary") == ("src/app.py:summary", None)
