"""The ``tool`` broker: canonical advertisement, separate exact-name calls.

Mirrors the public server broker in ``lemoncrow.gateway.tools.broker`` while
keeping the dependency-free thin client self-contained. Selecting the core
profile must not expose a broker excluded by the generated allowlist. Calls
still flow through the normal :class:`~lemoncrow_client.dispatcher.Dispatcher`;
this module adds discovery, not a parallel execution path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .dispatcher import Dispatcher, ToolOutcome
from .errors import AgentAction, ClientError, ErrorCode, error_block
from .routing import ROUTES
from .surface import _mcp_tool_profile, load_surface, served_spec, tool_list

__all__ = ["BROKER_TOOL_NAME", "broker_advertised", "broker_enabled", "broker_spec", "handle"]

BROKER_TOOL_NAME: Final[str] = "tool"

_DESCRIPTION: Final[str] = (
    "Search or call a routed LemonCrow tool that LEMONCROW_MCP_TOOL_PROFILE=core "
    "keeps off tools/list to save schema tokens. A tool already listed in "
    "tools/list must be called directly instead -- this exists only for the "
    "rest.\n"
    "- action='search': query (substring, optional) -> matching hidden tools.\n"
    "- action='call': name (exact) + arguments -> that tool's normal result."
)

_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "call"]},
        "query": {"type": "string", "description": "action=search: substring matched against name and description."},
        "name": {"type": "string", "description": "action=call: the exact hidden tool name."},
        "arguments": {"type": "object", "description": "action=call: arguments for the target tool."},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def broker_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether exact-name broker calls are supported in the active profile."""
    return _mcp_tool_profile(env) == "core"


def broker_advertised(env: Mapping[str, str] | None = None) -> bool:
    """Advertisement honors canonical visibility independently of callability."""
    return broker_enabled(env) and bool(load_surface().get(BROKER_TOOL_NAME, {}).get("visibleToLlm", False))


def broker_spec() -> dict[str, Any]:
    """The ``tools/list`` entry for the broker itself."""
    return {"name": BROKER_TOOL_NAME, "description": _DESCRIPTION, "inputSchema": dict(_INPUT_SCHEMA)}


def _hidden_names(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Routed tools hidden from ``tools/list`` by the active profile.

    Allows discovering and calling any routed tool not already exposed in tools/list.
    """
    visible = {entry["name"] for entry in tool_list(env=env)}
    return frozenset(set(ROUTES) - visible)


def _format_schema(schema: Any) -> str:
    """Render an inputSchema dict as a compact readable parameter list."""
    if not isinstance(schema, Mapping):
        return ""
    props = schema.get("properties")
    if not isinstance(props, Mapping) or not props:
        return ""
    required: set[str] = set(schema.get("required") or [])
    lines = []
    for param, info in props.items():
        if not isinstance(info, Mapping):
            continue
        typ = str(info.get("type") or "any")
        desc = str(info.get("description") or "")
        req = " (required)" if param in required else ""
        line = f"    {param}: {typ}{req}"
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


def _search(query: str, env: Mapping[str, str] | None) -> ToolOutcome:
    surface = load_surface()
    needle = query.strip().lower()
    parts: list[str] = []
    for name in sorted(_hidden_names(env)):
        description, schema = served_spec(name, surface.get(name, {}))
        haystack = f"{name} {description}".lower()
        if not needle or needle in haystack:
            schema_text = _format_schema(schema)
            entry = f"{name}: {description}"
            if schema_text:
                entry += f"\n  arguments:\n{schema_text}"
            parts.append(entry)
    text = "\n\n".join(parts) if parts else "no hidden tool matches that query"
    return ToolOutcome(content=({"type": "text", "text": text},))


def _refusal(message: str) -> ToolOutcome:
    error = ClientError(ErrorCode.PAYLOAD_INVALID, message, action=AgentAction.FIX_REQUEST)
    return ToolOutcome(content=(error_block(error),), is_error=True)


def _call(
    name: str, arguments: Mapping[str, Any], dispatcher: Dispatcher, env: Mapping[str, str] | None
) -> ToolOutcome:
    if not name:
        return _refusal("action='call' requires 'name'")
    advertised = {entry["name"] for entry in tool_list(env=env)}
    if name in advertised:
        return _refusal(f"{name!r} is already exposed; call it directly")
    if name not in _hidden_names(env):
        return _refusal(f"unknown or unavailable tool: {name}")
    return dispatcher.call(name, arguments)


def handle(
    arguments: Mapping[str, Any], *, dispatcher: Dispatcher, env: Mapping[str, str] | None = None
) -> ToolOutcome:
    """Run one ``tool`` broker call. The only entry point this module needs."""
    action = str(arguments.get("action") or "")
    if action == "search":
        return _search(str(arguments.get("query") or ""), env)
    if action == "call":
        raw_target_arguments = arguments.get("arguments")
        target_arguments: Mapping[str, Any] = raw_target_arguments if isinstance(raw_target_arguments, Mapping) else {}
        return _call(str(arguments.get("name") or ""), target_arguments, dispatcher, env)
    return _refusal(f"unknown broker action: {action!r}")
