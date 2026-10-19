"""Resolved optional third-party runtime dependencies.

These packages are declared as required dependencies, but the imports stay
defensive so a partial install degrades gracefully instead of crashing at import
time. The try/except rebinding pattern (name imported in ``try``, replaced by a
stub/``None`` in ``except``) is incompatible with mypyc, so it lives in this open,
uncompiled module -- the compiled ``pro`` capabilities import the already-resolved
names from here. Kept out of mypyc compilation via the ``no-redef`` marker (see
``hatch_build._NO_REDEF_RE``); it carries no IP.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

try:
    from blake3 import blake3
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    blake3: Any = None  # type: ignore[no-redef]

try:
    from git import Repo
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    Repo: Any = None  # type: ignore[no-redef]

_retry_impl: Any = None
_stop_after_attempt_impl: Any = None
_wait_exponential_impl: Any = None
try:
    from tenacity import retry as _tenacity_retry
    from tenacity import stop_after_attempt as _tenacity_stop_after_attempt
    from tenacity import wait_exponential as _tenacity_wait_exponential

    _retry_impl = _tenacity_retry
    _stop_after_attempt_impl = _tenacity_stop_after_attempt
    _wait_exponential_impl = _tenacity_wait_exponential
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")

_P = ParamSpec("_P")
_R = TypeVar("_R")


def retry(*args: Any, **kwargs: Any) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    if _retry_impl is None:
        return lambda fn: fn
    return cast(Any, _retry_impl)(*args, **kwargs)


def stop_after_attempt(attempts: int) -> Any:
    if _stop_after_attempt_impl is None:
        return None
    return _stop_after_attempt_impl(attempts)


def wait_exponential(**kwargs: Any) -> Any:
    if _wait_exponential_impl is None:
        return None
    return _wait_exponential_impl(**kwargs)


try:
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    Counter: Any = None  # type: ignore[no-redef]
    Histogram: Any = None  # type: ignore[no-redef]

try:
    import pybreaker
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    pybreaker: Any = None  # type: ignore[no-redef]

__all__ = [
    "Counter",
    "Histogram",
    "Repo",
    "blake3",
    "pybreaker",
    "retry",
    "stop_after_attempt",
    "wait_exponential",
]
