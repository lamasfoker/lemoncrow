"""Optional seam to LemonCrow's private owned Runtime implementation.

Public Runtime Lite never requires this module's provider target. The private
monorepo keeps the implementation for Hosted/Enterprise engineering and
benchmarks; public mirrors/releases omit it and this seam fails explicitly.
"""

from __future__ import annotations

import importlib
from typing import Any


class PrivateRuntimeUnavailable(RuntimeError):
    pass


def run_private_coding_engine(**kwargs: Any) -> int:
    try:
        module = importlib.import_module("lemoncrow.gateway.cli.coding_engine")
    except ModuleNotFoundError as exc:
        raise PrivateRuntimeUnavailable(
            "private owned Runtime execution is not installed in this public build"
        ) from exc
    runner = getattr(module, "run_coding_engine", None)
    if runner is None:
        raise PrivateRuntimeUnavailable("private owned Runtime provider is incomplete")
    return int(runner(**kwargs))


__all__ = ["PrivateRuntimeUnavailable", "run_private_coding_engine"]
