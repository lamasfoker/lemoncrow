"""Cooperative cancellation for database reads running in worker threads."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event

from .errors import ErrorCode, ServerError


@dataclass
class ReadBudget:
    deadline: float | None = None
    cancelled: Event = field(default_factory=Event)

    def interrupted(self) -> bool:
        return self.cancelled.is_set() or (self.deadline is not None and time.monotonic() >= self.deadline)

    def check(self) -> None:
        if self.cancelled.is_set():
            raise ServerError(ErrorCode.CANCELLED, "database read was cancelled with its request")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ServerError(ErrorCode.DEADLINE_EXCEEDED, "database read exceeded the request deadline")


_CURRENT: ContextVar[ReadBudget | None] = ContextVar("lemoncrow_read_budget", default=None)


def current_read_budget() -> ReadBudget | None:
    return _CURRENT.get()


@contextmanager
def read_budget(budget: ReadBudget) -> Iterator[None]:
    token = _CURRENT.set(budget)
    try:
        yield
    finally:
        _CURRENT.reset(token)
