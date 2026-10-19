"""Normative execution ownership for the public Runtime products.

There is intentionally no default route. A new public tool must be assigned an
owner here before either MCP surface may expose it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Placement(StrEnum):
    LOCAL_LITE = "LOCAL_LITE"
    HOSTED = "HOSTED"
    HOSTED_ADMIN = "HOSTED_ADMIN"
    NOT_EXPOSED = "NOT_EXPOSED"


TOOL_PLACEMENT: Final[dict[str, Placement]] = {
    "bash": Placement.LOCAL_LITE,
    "edit": Placement.LOCAL_LITE,
    "grep": Placement.LOCAL_LITE,
    "read": Placement.LOCAL_LITE,
    "scan": Placement.LOCAL_LITE,
    "codemod": Placement.LOCAL_LITE,
    "blame": Placement.LOCAL_LITE,
    "code_search": Placement.LOCAL_LITE,
    "relations": Placement.LOCAL_LITE,
    "graph": Placement.LOCAL_LITE,
    "index": Placement.NOT_EXPOSED,
    "sql": Placement.NOT_EXPOSED,
    "search": Placement.HOSTED,
    "context": Placement.HOSTED,
    "memory": Placement.HOSTED,
    "verify": Placement.HOSTED,
    "trace": Placement.HOSTED,
    "rescue": Placement.HOSTED,
    "compact": Placement.HOSTED,
    "orient": Placement.HOSTED,
    "web_fetch": Placement.LOCAL_LITE,
    "statusline_segment": Placement.HOSTED,
    "review_rationale": Placement.HOSTED,
    "review_evidence": Placement.HOSTED,
    "review_feedback_addressed": Placement.HOSTED,
    "review_guidance": Placement.HOSTED,
    "cache": Placement.HOSTED_ADMIN,
    "mcp": Placement.NOT_EXPOSED,
    "agent": Placement.NOT_EXPOSED,
    "workflow": Placement.NOT_EXPOSED,
}

LOCAL_LITE_TOOLS: Final[tuple[str, ...]] = tuple(
    name for name, placement in TOOL_PLACEMENT.items() if placement is Placement.LOCAL_LITE
)

# Model-facing allowlist; must equal lemoncrow.core.environment.LLM_VISIBLE_TOOLS
# (the dependency-free client cannot import it; a test pins parity). Other
# LOCAL_LITE tools stay routable but are not advertised in tools/list.
LITE_VISIBLE_TOOLS: Final[frozenset[str]] = frozenset({"bash", "code_search", "edit", "read", "web_fetch"})


def placement_for(name: str) -> Placement:
    """Return an explicit owner; unknown names are never routed by fallback."""

    try:
        return TOOL_PLACEMENT[name]
    except KeyError as exc:
        raise KeyError(f"tool has no Runtime placement: {name}") from exc


__all__ = ["LITE_VISIBLE_TOOLS", "LOCAL_LITE_TOOLS", "TOOL_PLACEMENT", "Placement", "placement_for"]
