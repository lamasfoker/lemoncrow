"""One server per state directory.

Two servers on one ``--directory`` each run an index worker and maintenance
against the same SQLite files: they duplicate every deferred build, contend
for every write lock, and a restart of either failed with "database is
locked". The claim is an advisory lock held for the server process's life.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import BinaryIO, Final

__all__ = ["LOCK_NAME", "StateRootBusy", "claim_state_root"]

LOCK_NAME: Final[str] = "server.lock"
_POLL_S: Final[float] = 0.2


class StateRootBusy(RuntimeError):
    """Another live server holds the state directory."""


def _try_lock(handle: BinaryIO) -> bool:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _holder(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return f" ({text})" if text else ""


def claim_state_root(state_root: Path, *, port: int, wait_s: float = 15.0) -> BinaryIO:
    """Hold *state_root* exclusively; the returned handle keeps the claim.

    A restarting server's predecessor can still be exiting, so a held claim
    is waited on for *wait_s* before :class:`StateRootBusy`. Process exit
    releases the lock; the file stays, since unlinking a lock file another
    process has open lets two holders appear.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / LOCK_NAME
    handle = path.open("a+b")
    if sys.platform == "win32":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\n")
            handle.flush()
    deadline = time.monotonic() + wait_s
    while not _try_lock(handle):
        if time.monotonic() >= deadline:
            handle.close()
            raise StateRootBusy(
                f"another LemonCrow server is serving {state_root}{_holder(path)}; "
                "stop it or start this one with a different --directory"
            )
        time.sleep(_POLL_S)
    if sys.platform != "win32":
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} port={port}\n".encode())
        handle.flush()
    return handle
