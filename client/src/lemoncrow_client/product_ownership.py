"""Product ownership for the public LemonCrow thin-client command surface."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ProductOwner(StrEnum):
    CORE = "core"
    REVIEW = "review"
    RUNTIME = "runtime"


COMMAND_OWNERSHIP: Final[dict[str, ProductOwner]] = {
    "auth": ProductOwner.CORE,
    "status": ProductOwner.CORE,
    "uninstall": ProductOwner.CORE,
    "update": ProductOwner.CORE,
    "target": ProductOwner.CORE,
    "review": ProductOwner.REVIEW,
    "code": ProductOwner.RUNTIME,
    "run": ProductOwner.RUNTIME,
    "session": ProductOwner.RUNTIME,
    "context": ProductOwner.RUNTIME,
    "with": ProductOwner.RUNTIME,
    "runtime": ProductOwner.RUNTIME,
    "rt": ProductOwner.RUNTIME,
    "audit": ProductOwner.CORE,
}

__all__ = ["COMMAND_OWNERSHIP", "ProductOwner"]
