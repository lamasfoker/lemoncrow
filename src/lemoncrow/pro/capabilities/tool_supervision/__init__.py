"""Tool supervision capability — public API.

The package is shared by Review evidence helpers and the Runtime supervision
capability. Keep the public API lazy so importing a Review-only submodule such
as ``edit_impact`` does not pull the Runtime circuit-breaker/telemetry stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AnomalyAlert",
    "CircuitBreaker",
    "CircuitState",
    "SupervisionMetrics",
    "ToolObservation",
    "ToolSupervisionCapability",
]

_LAZY_EXPORTS = {
    "ToolSupervisionCapability": (".capability", "ToolSupervisionCapability"),
    "CircuitBreaker": (".circuit_breaker", "CircuitBreaker"),
    "AnomalyAlert": (".models", "AnomalyAlert"),
    "CircuitState": (".models", "CircuitState"),
    "SupervisionMetrics": (".models", "SupervisionMetrics"),
    "ToolObservation": (".models", "ToolObservation"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, symbol = target
    value = getattr(import_module(module_name, __name__), symbol)
    globals()[name] = value
    return value
