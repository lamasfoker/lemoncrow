from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_trace_hooks_late_bind_runtime_and_limits(monkeypatch) -> None:
    marker = object()

    def fake_runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", fake_runtime)
    monkeypatch.setattr(mcp_server, "_MAX_TRACE_FILES", 7)
    monkeypatch.setattr(mcp_server, "_MAX_TRACE_FILE_BYTES", 1234)
    hooks = mcp_server._trace_handler_hooks()
    assert hooks.runtime is fake_runtime
    assert hooks.runtime() is marker
    assert hooks.max_trace_files == 7
    assert hooks.max_trace_file_bytes == 1234
