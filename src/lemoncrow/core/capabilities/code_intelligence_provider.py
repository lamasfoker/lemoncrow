"""Public discovery seam for private advanced code intelligence.

Runtime Lite is a separate OSS implementation and never flows through this
factory. Hosted/Enterprise and guarded private-development code call this module when
they need the advanced engine. The private enterprise runtime supplies the actual
implementation.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from lemoncrow.core.capabilities.code_context_contract import CodeIntelligenceProvider

PRIVATE_CODE_INTELLIGENCE_UNAVAILABLE_MESSAGE: Final[str] = "Private code intelligence is not installed/available."
# Compatibility alias for internal callers; Pro is a plan, not a Runtime architecture.
PRO_ENGINE_UNAVAILABLE_MESSAGE: Final[str] = PRIVATE_CODE_INTELLIGENCE_UNAVAILABLE_MESSAGE


class ProEngineUnavailable(RuntimeError):
    """Raised when an advanced code-intelligence feature has no private provider."""


def _private_provider() -> ModuleType:
    try:
        return import_module("lemoncrow_enterprise_runtime.code_intelligence.provider")
    except ModuleNotFoundError as exc:
        if exc.name and (
            exc.name == "lemoncrow_enterprise_runtime" or exc.name.startswith("lemoncrow_enterprise_runtime.")
        ):
            raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE) from exc
        raise


def _is_private_missing(exc: ModuleNotFoundError) -> bool:
    name = exc.name or ""
    return (
        name == "lemoncrow_enterprise_runtime"
        or name.startswith("lemoncrow_enterprise_runtime.")
        or name.startswith("lemoncrow.pro.capabilities.code_context.")
    )


def _call_private(module: ModuleType, name: str, *args: Any, **kwargs: Any) -> Any:
    target = getattr(module, name, None)
    if not callable(target):
        raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE)
    try:
        return target(*args, **kwargs)
    except ModuleNotFoundError as exc:
        if _is_private_missing(exc):
            raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE) from exc
        raise


def private_code_intelligence_available() -> bool:
    try:
        require_private_code_intelligence()
    except ProEngineUnavailable:
        return False
    return True


def require_private_code_intelligence() -> None:
    _call_private(_private_provider(), "ensure_available")


def create_code_intelligence_provider(
    repo_root: str | Path,
    **kwargs: Any,
) -> CodeIntelligenceProvider:
    module = _private_provider()
    return _call_private(module, "create_provider", repo_root, **kwargs)


def advanced_code_intelligence_helper(name: str, *args: Any, **kwargs: Any) -> Any:
    """Invoke one explicitly private compatibility helper through one seam."""

    module = _private_provider()
    helper = getattr(module, name, None)
    if not callable(helper):
        raise ProEngineUnavailable(f"{PRO_ENGINE_UNAVAILABLE_MESSAGE} Missing helper: {name}")
    try:
        return helper(*args, **kwargs)
    except ModuleNotFoundError as exc:
        if _is_private_missing(exc):
            raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE) from exc
        raise


__all__ = [
    "PRO_ENGINE_UNAVAILABLE_MESSAGE",
    "ProEngineUnavailable",
    "advanced_code_intelligence_helper",
    "create_code_intelligence_provider",
    "private_code_intelligence_available",
    "require_private_code_intelligence",
]
