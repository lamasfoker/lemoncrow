"""Modules a LemonCrow build may deliberately not ship, told apart from real faults.

The Review-only local engine (``server/review-engine-files.txt``) ships an explicit
subset of the first-party packages. Code it ships imports, lazily, some modules it
does not: they read records only Runtime writes (agent ledgers, the author-session
journal, ...) or belong to Runtime itself. Such a call site catches
``ModuleNotFoundError`` and asks :func:`module_absent` whether the miss is that module
itself, the declared and accepted absence, and degrades visibly; anything else (a
missing third-party dependency, or a shipped module whose own import is broken) is a
fault and must propagate. ``tests/gateway/test_review_engine_artifact.py`` keeps every
such module on the engine's explicit optional list, ``server/review-engine-optional.txt``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["module_absent", "skip_if_absent"]

# lc-debt: a new call site that imports an optional module unguarded still answers 500;
# only its own test and the engine smoke catch it. Map an optional module's
# ModuleNotFoundError to a typed 501 in the HTTP error middleware as a safety net.


def module_absent(exc: ModuleNotFoundError, module: str) -> bool:
    """Whether *exc* says *module* (or a package containing it) is not installed."""

    missing = exc.name or ""
    return bool(missing) and (module == missing or module.startswith(missing + "."))


@contextmanager
def skip_if_absent(module: str) -> Iterator[None]:
    """Run an optional step that imports *module*; skip the rest of it where *module* is absent."""

    try:
        yield
    except ModuleNotFoundError as exc:
        if not module_absent(exc, module):
            raise
