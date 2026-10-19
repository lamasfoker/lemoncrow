"""Runtime-only UI/diagnostic projections used by the shared local shell.

This module is deliberately excluded from the Review engine artifact. Shared
composition may import it only after proving the Runtime product is enabled.
"""

from __future__ import annotations

from typing import Any


def advertised_runtime_mcp_tools() -> list[dict[str, Any]]:
    from lemoncrow.gateway.tools.registry import advertised_tools

    return [
        {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
        }
        for tool in advertised_tools()
    ]


def hosted_runtime_capabilities() -> dict[str, bool]:
    """Capabilities the current remote Runtime wire surface can truthfully expose.

    Keep absent/unwired managed features explicitly false so discovery and
    product copy cannot silently relabel the old local-private process as Hosted.
    """

    return {
        "remote_code_search": True,
        "semantic_hybrid_search": True,
        "server_graph": True,
        "revision_bound_views": True,
        "blob_source_sync": True,
        "tenant_scoped_auth": True,
        "cross_workspace_routing": False,
        "owned_loop": False,
        "provider_routing": False,
        "budget_spend_controls": False,
        "prompt_cache_orchestration": False,
    }
