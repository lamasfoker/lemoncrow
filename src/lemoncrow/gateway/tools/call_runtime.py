"""JSON-RPC ``tools/call`` adapter over :class:`ToolCallRuntime`.

The runtime itself (:mod:`lemoncrow.gateway.tools.runtime_call`) knows nothing
about JSON-RPC.  This module owns the translation from a ``tools/call``
``params`` object into a :class:`RuntimeCall`, plus the process-default runtime
used by host surfaces that are not the stdio composition root.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lemoncrow.core.foundation.runtime_trace import is_trace_id, new_trace_id
from lemoncrow.gateway.tools.errors import ToolProtocolError
from lemoncrow.gateway.tools.invocation import _normalized_arguments
from lemoncrow.gateway.tools.runtime_call import (
    RuntimeCall,
    RuntimeResult,
    ToolCallRun,
    ToolCallRuntime,
    ToolCallRuntimeHooks,
    commit_run_trace,
    finalize_run_payload,
)

#: ``_meta`` key a caller uses to hand its trace id to the runtime.
META_CALL_ID = "lemoncrow-call-id"
_META_PROJECT_ID_KEYS = ("lemoncrow-project-id", "projectId", "project_id", "workspaceId", "workspace_id")
_META_PROJECT_PATH_KEYS = ("mcp-project-path", "projectPath", "project_path", "projectRoot", "project_root")


def _meta_workspace(meta: Mapping[str, Any]) -> str | None:
    for key in (*_META_PROJECT_ID_KEYS, *_META_PROJECT_PATH_KEYS):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def runtime_call_from_jsonrpc(params: Any) -> RuntimeCall:
    """Translate a JSON-RPC ``tools/call`` params object into a RuntimeCall."""
    if not isinstance(params, Mapping):
        params = {}
    meta = params.get("_meta")
    meta = meta if isinstance(meta, Mapping) else {}
    call_id = meta.get(META_CALL_ID)
    return RuntimeCall(
        tool=str(params.get("name") or ""),
        arguments=_normalized_arguments(params),
        call_id=call_id if isinstance(call_id, str) and is_trace_id(call_id) else new_trace_id(),
        workspace_id=_meta_workspace(meta),
    )


def run_tool_call(rid: Any, params: Any, *, hooks: ToolCallRuntimeHooks) -> ToolCallRun:
    """Compatibility wrapper: JSON-RPC params -> :meth:`ToolCallRuntime.run`."""
    return ToolCallRuntime(hooks).run(runtime_call_from_jsonrpc(params), correlation=rid)


def finalize_tool_payload(run: ToolCallRun) -> dict[str, Any]:
    """Finalize one synchronous run into host-neutral tool content."""
    return finalize_run_payload(run)


_DefaultHooksFactory = Callable[[], ToolCallRuntimeHooks]
_default_hooks_factory: _DefaultHooksFactory | None = None


def configure_default_tool_runtime(factory: _DefaultHooksFactory) -> None:
    """Install the process default hook factory used by non-MCP host surfaces.

    The factory is intentionally late-bound: the legacy composition root can
    preserve runtime monkey-patching while consumers depend only on this module.
    """
    global _default_hooks_factory
    _default_hooks_factory = factory


def default_tool_runtime_configured() -> bool:
    return _default_hooks_factory is not None


def default_tool_runtime() -> ToolCallRuntime:
    factory = _default_hooks_factory
    if factory is None:
        raise RuntimeError("default tool runtime is not configured")
    return ToolCallRuntime(factory())


def invoke_default_runtime(call: RuntimeCall, *, correlation: Any = None) -> RuntimeResult:
    """Execute one typed call via the configured process-default runtime."""
    return default_tool_runtime().invoke(call, correlation=correlation)


def execute_default_tool_payload(rid: Any, params: Any) -> dict[str, Any]:
    """Execute and finalize one JSON-RPC ``tools/call`` via the configured runtime."""
    return invoke_default_runtime(runtime_call_from_jsonrpc(params), correlation=rid).payload


__all__ = [
    "META_CALL_ID",
    "RuntimeCall",
    "RuntimeResult",
    "ToolCallRun",
    "ToolCallRuntime",
    "ToolCallRuntimeHooks",
    "ToolProtocolError",
    "commit_run_trace",
    "configure_default_tool_runtime",
    "default_tool_runtime",
    "default_tool_runtime_configured",
    "execute_default_tool_payload",
    "finalize_tool_payload",
    "invoke_default_runtime",
    "run_tool_call",
    "runtime_call_from_jsonrpc",
]
