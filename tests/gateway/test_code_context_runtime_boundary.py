from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server


def test_mcp_cache_reset_delegates_to_canonical_runtime() -> None:
    source = inspect.getsource(mcp_server._reset_runtime_cache_for_testing)
    assert "_reset_code_context_cache()" in source
