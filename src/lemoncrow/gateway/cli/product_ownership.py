"""Machine-readable product ownership for the visible LemonCrow CLI."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ProductOwner(StrEnum):
    CORE = "core"
    REVIEW = "review"
    RUNTIME = "runtime"


# Every visible top-level command must appear here. Hidden/internal compatibility
# commands are intentionally excluded until/unless they become user-facing.
COMMAND_OWNERSHIP: Final[dict[str, ProductOwner]] = {
    "agent": ProductOwner.RUNTIME,
    "auth": ProductOwner.CORE,
    "benchmark": ProductOwner.RUNTIME,
    "code": ProductOwner.RUNTIME,
    "completions": ProductOwner.CORE,
    "context": ProductOwner.RUNTIME,
    "dashboard": ProductOwner.RUNTIME,
    "debt": ProductOwner.RUNTIME,
    "doctor": ProductOwner.CORE,
    "help": ProductOwner.CORE,
    "host": ProductOwner.RUNTIME,
    "init": ProductOwner.RUNTIME,
    "map": ProductOwner.RUNTIME,
    "index": ProductOwner.RUNTIME,
    "install": ProductOwner.RUNTIME,
    "mcp": ProductOwner.RUNTIME,
    "model": ProductOwner.RUNTIME,
    "review": ProductOwner.REVIEW,
    "runtime": ProductOwner.RUNTIME,
    "rt": ProductOwner.RUNTIME,
    "run": ProductOwner.RUNTIME,
    "service": ProductOwner.RUNTIME,
    "session": ProductOwner.RUNTIME,
    "settings": ProductOwner.CORE,
    "status": ProductOwner.CORE,
    "skill": ProductOwner.RUNTIME,
    "swarm": ProductOwner.RUNTIME,
    "telemetry": ProductOwner.RUNTIME,
    "trace": ProductOwner.RUNTIME,
    "uninstall": ProductOwner.CORE,
    "update": ProductOwner.CORE,
    "usage": ProductOwner.CORE,
    "with": ProductOwner.RUNTIME,
    "worker": ProductOwner.RUNTIME,
    "work": ProductOwner.RUNTIME,
}

# Product ownership and command ergonomics are separate concerns. Runtime owns
# all of these capabilities, while a small set of frequent user jobs are
# intentionally promoted to the root CLI as first-class spellings. The same
# Click command object is also available beneath `lc runtime` / `lc rt`.
PRODUCT_ALIASES: Final[frozenset[str]] = frozenset({"rt"})
RUNTIME_PROMOTED_COMMANDS: Final[frozenset[str]] = frozenset({"code", "host", "run", "session", "context"})
RUNTIME_ENGINE_SHORTCUTS: Final[frozenset[str]] = frozenset({"with"})

RUNTIME_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    name
    for name, owner in COMMAND_OWNERSHIP.items()
    if owner is ProductOwner.RUNTIME and name not in {"runtime", *PRODUCT_ALIASES}
)
RUNTIME_NAMESPACED_ONLY_COMMANDS: Final[frozenset[str]] = (
    RUNTIME_COMMAND_NAMES - RUNTIME_PROMOTED_COMMANDS - RUNTIME_ENGINE_SHORTCUTS
)

VISIBLE_COMMAND_OWNERSHIP: Final[dict[str, ProductOwner]] = {
    name: owner
    for name, owner in COMMAND_OWNERSHIP.items()
    if name not in RUNTIME_NAMESPACED_ONLY_COMMANDS and name not in PRODUCT_ALIASES
}

__all__ = [
    "COMMAND_OWNERSHIP",
    "PRODUCT_ALIASES",
    "RUNTIME_COMMAND_NAMES",
    "RUNTIME_ENGINE_SHORTCUTS",
    "RUNTIME_NAMESPACED_ONLY_COMMANDS",
    "RUNTIME_PROMOTED_COMMANDS",
    "VISIBLE_COMMAND_OWNERSHIP",
    "ProductOwner",
]
