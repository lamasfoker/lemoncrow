"""Shared server-engine wiring used by every composition root.

Tool dispatch is not wired here: server-side tool execution is hosted-only
(``lemoncrow_server.bootstrap.build_dispatcher``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .index.contracts import IndexBackend
from .index.memory import build_memory_backend
from .index.sqlite import build_sqlite_backend
from .workspace import ViewMaterializer

__all__ = ["EngineConfig", "EngineLimits", "build_backend", "build_workspace"]


class EngineLimits(Protocol):
    max_indexed_content_bytes: int
    inline_rebuild_budget: int
    max_materialized_views: int
    materialized_bytes_quota: int
    tool_deadline_s: float


class EngineConfig(Protocol):
    index_path: Path | None
    workspace_root: Path | None
    limits: EngineLimits


def build_backend(config: EngineConfig) -> IndexBackend:
    """Build the deployment-neutral memory/SQLite index backend."""
    if config.index_path is not None:
        config.index_path.parent.mkdir(parents=True, exist_ok=True)
        return build_sqlite_backend(
            config.index_path,
            content_size_cap=config.limits.max_indexed_content_bytes,
            inline_rebuild_budget=config.limits.inline_rebuild_budget,
            # New shared server stores use compressed FTS postings; existing
            # normalized stores remain readable unchanged until an explicit
            # offline conversion is performed.
            compact=None,
        )
    return build_memory_backend(
        content_size_cap=config.limits.max_indexed_content_bytes,
        inline_rebuild_budget=config.limits.inline_rebuild_budget,
    )


def build_workspace(
    config: EngineConfig,
    backend: IndexBackend,
    *,
    clock: Callable[[], float] = time.time,
) -> ViewMaterializer:
    """Build the materialized tree cache every server-side tool answers from."""
    return ViewMaterializer(
        backend,
        root=config.workspace_root,
        content_size_cap=config.limits.max_indexed_content_bytes,
        max_views=config.limits.max_materialized_views,
        quota_bytes=config.limits.materialized_bytes_quota,
        eviction_grace_s=config.limits.tool_deadline_s,
        clock=clock,
    )
