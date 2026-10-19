from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server


def test_summary_wrapper_supplies_live_mcp_dependencies() -> None:
    source = inspect.getsource(mcp_server._read_summary_response)
    assert "_canonical_read_summary_response" in source
    assert "semantic_memory_root=_lemoncrow_root()" in source
    assert "render_outline=_render_read_outline_md" in source


def test_batch_budget_wrapper_uses_live_smart_read() -> None:
    source = inspect.getsource(mcp_server._apply_batch_read_budget)
    assert "_canonical_apply_batch_read_budget" in source
    assert "outline_reader=_smart_read_single" in source
