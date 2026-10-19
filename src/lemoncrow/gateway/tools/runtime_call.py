"""ToolCallRuntime: the one transport-neutral execution API for tool calls.

Every host surface (stdio MCP, HTTP MCP, the LemonCrow server's registry
dispatch) translates its own request framing into a :class:`RuntimeCall`,
calls :meth:`ToolCallRuntime.invoke` (or :meth:`ToolCallRuntime.run` when it
must apply its own error/deferral framing), and translates the result back.

This module must not import any transport: no MCP/JSON-RPC ids, no aiohttp,
no HTTP headers.  ``tests/gateway/test_runtime_call_boundary.py`` enforces it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lemoncrow.core.foundation.runtime_budget import RuntimeBudget
from lemoncrow.core.foundation.runtime_trace import (
    TraceHandle,
    TraceMode,
    begin_trace,
    new_trace_id,
)
from lemoncrow.gateway.tools.errors import ToolArgumentError, ToolProtocolError
from lemoncrow.gateway.tools.execution import execute_prepared_tool_call
from lemoncrow.gateway.tools.invocation import ToolCallPreparationError, prepare_tool_call
from lemoncrow.gateway.tools.lifecycle import ToolCallLifecycle, ToolLifecycleHooks
from lemoncrow.gateway.tools.state import tool_call_images
from lemoncrow.gateway.tools.workspace import clear_request_project, extract_request_project, set_request_project


@dataclass(frozen=True, slots=True)
class RuntimeCall:
    """One tool call, free of transport concepts."""

    tool: str
    arguments: Mapping[str, Any]
    call_id: str = field(default_factory=new_trace_id)
    workspace_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    budget: RuntimeBudget | None = None
    trace_mode: TraceMode | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeUsage:
    duration_ms: int = 0
    output_chars: int = 0
    est_output_tokens: int = 0
    remote_routed: bool = False


@dataclass(slots=True)
class RuntimeResult:
    """Finalized, host-neutral result of one call."""

    payload: dict[str, Any]
    trace_id: str
    is_error: bool
    usage: RuntimeUsage
    decisions: tuple[dict[str, Any], ...] = ()
    pending: Any = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallRuntimeHooks:
    broker_spec: dict[str, Any]
    remote_tools: frozenset[str]
    dispatch_remote: Callable[[str, dict[str, Any]], Any]
    get_ledger: Callable[[], Any]
    prepare_model_recommendation: Callable[..., Any]
    finalize_model_recommendation: Callable[..., Any]
    lifecycle_hooks: Callable[[], ToolLifecycleHooks]
    record_session_cwd: Callable[[str, dict[str, Any]], None]
    is_deferred_result: Callable[[Any], bool]


@dataclass(slots=True)
class ToolCallRun:
    lifecycle: ToolCallLifecycle
    result: Any = None
    error: Exception | None = None
    deferred: bool = False
    trace: TraceHandle | None = None


def _payload_chars(payload: Mapping[str, Any]) -> int:
    total = 0
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                total += len(block["text"])
    return total


def commit_run_trace(
    run: ToolCallRun, payload: Mapping[str, Any] | None, *, error: BaseException | None = None
) -> None:
    """Record the finalized outcome of ``run`` and persist its trace once."""
    handle = run.trace
    if handle is None or handle.committed:
        return
    trace = handle.trace
    if handle.owned:
        chars = _payload_chars(payload) if payload is not None else 0
        trace.note(
            "outcome",
            is_error=bool(error is not None or (payload is not None and payload.get("isError"))),
            output_chars=chars,
            est_output_tokens=chars // 4,
            deferred=run.deferred,
            **({"error_type": type(error).__name__} if error is not None else {}),
        )
    handle.commit()


class ToolCallRuntime:
    """Prepare, execute, trace and finalize tool calls for any host."""

    __slots__ = ("_hooks",)

    def __init__(self, hooks: ToolCallRuntimeHooks) -> None:
        self._hooks = hooks

    @property
    def hooks(self) -> ToolCallRuntimeHooks:
        return self._hooks

    def run(self, call: RuntimeCall, *, correlation: Any = None) -> ToolCallRun:
        """Execute ``call`` and return the unfinalized run.

        ``correlation`` is the host's opaque id for accounting/response framing
        (defaults to ``call.call_id``). Preparation/workspace faults raise
        :class:`ToolProtocolError` before a lifecycle exists; handler failures
        are returned on the run. The caller must finalize via
        :func:`finalize_run_payload` or :func:`commit_run_trace`.
        """
        hooks = self._hooks
        handle = begin_trace(
            trace_id=call.call_id,
            writer="runtime",
            tool=call.tool,
            arguments=call.arguments,
            mode=call.trace_mode,
            session_id=call.session_id,
            workspace_id=call.workspace_id,
        )
        if handle.owned and call.budget is not None:
            handle.trace.note("metadata", budget=call.budget.to_payload())
        with handle.active():
            tool_call_images.value = []
            try:
                prepared = prepare_tool_call(
                    {"name": call.tool, "arguments": dict(call.arguments)},
                    broker_spec=hooks.broker_spec,
                    remote_tools=hooks.remote_tools,
                )
            except ToolCallPreparationError as exc:
                handle.trace.note("outcome", is_error=True, error_type="protocol", protocol_code=exc.code)
                handle.commit()
                raise ToolProtocolError(exc.code, str(exc)) from exc

            args = prepared.args
            try:
                project = call.workspace_id or extract_request_project({}, args)
                prior_project = set_request_project(project)
            except ToolArgumentError as exc:
                handle.trace.note("outcome", is_error=True, error_type="protocol", protocol_code=-32602)
                handle.commit()
                raise ToolProtocolError(-32602, str(exc)) from exc

            hooks.record_session_cwd(prepared.name, args)
            lifecycle = ToolCallLifecycle(
                rid=call.call_id if correlation is None else correlation,
                name=prepared.name,
                args=args,
                remote_routed=prepared.remote_routed,
                call_started=time.perf_counter(),
                hooks=hooks.lifecycle_hooks(),
            )
            try:
                try:
                    outcome = execute_prepared_tool_call(
                        prepared,
                        dispatch_remote=hooks.dispatch_remote,
                        get_ledger=hooks.get_ledger,
                        prepare_model_recommendation=hooks.prepare_model_recommendation,
                        finalize_model_recommendation=hooks.finalize_model_recommendation,
                    )
                except Exception as exc:
                    return ToolCallRun(lifecycle=lifecycle, error=exc, trace=handle)

                lifecycle.duration_ms = outcome.duration_ms
                lifecycle.ledger = outcome.ledger
                if handle.owned:
                    handle.trace.note(
                        "execution",
                        handler_ms=outcome.duration_ms,
                        remote_routed=outcome.remote_routed,
                    )
                return ToolCallRun(
                    lifecycle=lifecycle,
                    result=outcome.result,
                    deferred=hooks.is_deferred_result(outcome.result),
                    trace=handle,
                )
            finally:
                clear_request_project(prior_project)

    def invoke(self, call: RuntimeCall, *, correlation: Any = None) -> RuntimeResult:
        """Execute and finalize one call into a host-neutral :class:`RuntimeResult`."""
        started = time.perf_counter()
        run = self.run(call, correlation=correlation)
        payload = finalize_run_payload(run)
        chars = _payload_chars(payload)
        trace = run.trace.trace if run.trace is not None else None
        return RuntimeResult(
            payload=payload,
            trace_id=trace.trace_id if trace is not None else call.call_id,
            is_error=bool(payload.get("isError")),
            usage=RuntimeUsage(
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                output_chars=chars,
                est_output_tokens=chars // 4,
                remote_routed=bool(run.lifecycle.remote_routed),
            ),
            decisions=tuple(trace.decisions) if trace is not None else (),
        )


def finalize_run_payload(run: ToolCallRun) -> dict[str, Any]:
    """Finalize one synchronous run into host-neutral content and commit its trace."""
    scope = run.trace.active() if run.trace is not None else contextlib.nullcontext()
    try:
        with scope:
            if run.error is not None:
                payload = run.lifecycle.finalize_error_payload(run.error)
            elif run.deferred:
                payload = run.lifecycle.finalize_error_payload(
                    RuntimeError("deferred tool result is unsupported by synchronous payload execution")
                )
            else:
                payload = run.lifecycle.finalize_payload(run.result)
    except BaseException as exc:
        commit_run_trace(run, None, error=exc)
        raise
    commit_run_trace(run, payload, error=run.error)
    return payload


__all__ = [
    "RuntimeCall",
    "RuntimeResult",
    "RuntimeUsage",
    "ToolCallRun",
    "ToolCallRuntime",
    "ToolCallRuntimeHooks",
    "commit_run_trace",
    "finalize_run_payload",
]
