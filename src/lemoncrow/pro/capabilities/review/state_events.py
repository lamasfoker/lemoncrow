"""Authenticated Review invalidations driven by filesystem notifications."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from watchfiles import awatch

QUIET_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0


async def state_events(
    roots: list[Path],
    project: Callable[[set[tuple[Any, str]]], dict[str, Any]],
    *,
    event: str,
    watch_filter: Callable[[Any, str], bool] | None = None,
    settle: bool = False,
) -> AsyncIterator[str]:
    """Send an initial projection, changes and keepalives; close the watcher on exit.

    Source gets a second observation after a quiet second, preserving the
    automatic-capture stability check without repeatedly reading an idle tree.
    """
    watcher = cast(
        AsyncGenerator[set[tuple[Any, str]]],
        awatch(
            *roots, watch_filter=watch_filter, debounce=200, step=50, force_polling=False, recursive=True
        ).__aiter__(),
    )
    pending: asyncio.Future[set[tuple[Any, str]]] = asyncio.ensure_future(anext(watcher))
    previous: dict[str, Any] | None = None
    quiet = settle
    try:
        current = await asyncio.to_thread(project, set())
        if settle:
            current = {**current, "settled": False}
        previous = current
        yield f"event: {event}\ndata: {json.dumps(current, separators=(',', ':'))}\n\n"
        while True:
            ready, _ = await asyncio.wait({pending}, timeout=QUIET_SECONDS if quiet else HEARTBEAT_SECONDS)
            if ready:
                try:
                    changes = pending.result()
                except StopAsyncIteration:
                    return
                pending = asyncio.ensure_future(anext(watcher))
                current = await asyncio.to_thread(project, changes)
                quiet = settle
                if settle:
                    current = {**current, "settled": False}
                if current != previous or settle:
                    previous = current
                    yield f"event: {event}\ndata: {json.dumps(current, separators=(',', ':'))}\n\n"
            elif quiet:
                current = await asyncio.to_thread(project, set())
                # An edit arriving while the source was read is not a stable observation.
                if not pending.done():
                    current = {**current, "settled": current.get("fingerprint") == previous.get("fingerprint")}
                    previous = current
                    yield f"event: {event}\ndata: {json.dumps(current, separators=(',', ':'))}\n\n"
                    quiet = not current["settled"]
            else:
                yield ": keepalive\n\n"
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await watcher.aclose()
