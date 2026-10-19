"""Push out-of-band edits to the View ahead of the next call.

Correctness never depends on this module: every View-reading call already
syncs the working tree first (see :meth:`RemoteSession.call_tool`). This only
moves that work off the next call's critical path, and only when something
changed -- a check is one ``git status`` against the last synced fingerprint;
an unchanged tree sends nothing, a changed one re-hashes and uploads only what
changed. The same protocol serves hosted and same-host servers; with
``local_fs`` the server reads the bytes instead of receiving them.

What wakes a check (the MCP server syncs after this client's own ``bash``
itself, synchronously):

* Claude Code's hook after Edit/Write/Bash knocks on this client's socket
  (:func:`nudge_workspace`), debounced so a burst of saves is one check;
  other hosts send no nudge;
* while the session is in use, one check every :data:`IDLE_CHECK_S` catches
  editor and git changes no hook sees. An idle session stops checking.

This is the one module the packaging audit lets hold a socket or start a
``threading.Thread`` (worker pools elsewhere are shut down with their owners),
and it is narrow on purpose: an owner-only Unix *datagram* socket under the state
directory that accepts a one-word nudge -- never a network socket, never a
listening stream -- and two threads joined by :meth:`AutoSync.close`.
``LEMONCROW_AUTOSYNC=0`` turns it off.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "ACTIVE_WINDOW_S",
    "DEBOUNCE_S",
    "IDLE_CHECK_S",
    "AutoSync",
    "default_state_dir",
    "nudge_for_path",
    "nudge_workspace",
    "socket_dir",
]

#: Quiet time after a nudge before checking, so a burst of saves is one check.
DEBOUNCE_S: Final[float] = 0.5
#: The catch-all for changes no hook reports (an editor, a manual ``git``).
IDLE_CHECK_S: Final[float] = 30.0
#: The catch-all runs only this long after the last tool call.
ACTIVE_WINDOW_S: Final[float] = 600.0

#: Unix socket paths are capped at 104 (macOS) / 108 (Linux) bytes.
_MAX_SOCKET_PATH: Final[int] = 100
_NUDGE: Final[bytes] = b"sync"
_STOP: Final[bytes] = b"stop"
_LOG = logging.getLogger(__name__)

#: What a sync callback returns: ``True`` pushed, ``False`` nothing changed,
#: ``None`` could not check now (a call is in flight) -- try again shortly.
SyncResult = bool | None


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The client's state directory, resolved as :func:`load_config` does.

    Hooks run in their own process and must find the same socket directory
    without loading the whole client configuration. ``lc runtime mcp`` hands the client
    ``LEMONCROW_ROOT`` as its home when ``LEMONCROW_HOME`` is unset, so do we.
    """
    env = os.environ if environ is None else environ
    home = env.get("LEMONCROW_HOME", "").strip() or env.get("LEMONCROW_ROOT", "").strip()
    return Path(home) if home else Path(env.get("HOME", str(Path.home()))) / ".lemoncrow"


def socket_dir(repo_root: Path, state_dir: Path) -> Path:
    """Where clients serving ``repo_root`` listen: under the state directory.

    Inside the state directory so the client still writes nowhere but the
    worktree and its own state (a packaging-audit promise); one directory per
    workspace so a hook wakes only the clients serving that checkout.
    """
    key = hashlib.sha256(str(repo_root.resolve()).encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return state_dir / "sync" / key


def _owned(info: os.stat_result, *, private: bool) -> bool:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if not hasattr(os, "getuid"):
        return True
    return info.st_uid == _uid() and not info.st_mode & (0o077 if private else 0o002)


def _private_dir(path: Path) -> bool:
    """Create ``sync/<key>`` owner-only under a state directory we own.

    Every level is checked, not just the leaf: a directory another user could
    write to could have the leaf swapped out from under us. The state directory
    itself need only be ours and not world-writable (a user-private group is
    the Debian/Ubuntu default); the two levels this module creates are 0700.
    """
    try:
        if not _owned(path.parent.parent.lstat(), private=False):
            return False
        for level in (path.parent, path):
            level.mkdir(mode=0o700, exist_ok=True)
            if not _owned(level.lstat(), private=True):
                return False
    except OSError:
        return False
    return True


def nudge_workspace(repo_root: Path, state_dir: Path | None = None) -> int:
    """Wake every live client serving ``repo_root``; how many were reached.

    For hooks: never raises and never blocks. A socket nobody listens on any
    more (its process died) is removed on the way.
    """
    if not hasattr(socket, "AF_UNIX"):
        return 0
    root = state_dir or default_state_dir()
    roots = (root,) if root.name == "runtime" else (root / "runtime", root)
    try:
        targets = sorted({target for candidate in roots for target in socket_dir(repo_root, candidate).glob("*.sock")})
    except OSError:
        return 0
    woken = 0
    for target in targets:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as knock:
                knock.setblocking(False)
                knock.sendto(_NUDGE, str(target))
            woken += 1
        except (ConnectionRefusedError, FileNotFoundError):
            with contextlib.suppress(OSError):
                target.unlink()
        except OSError:
            continue  # a full queue already holds a nudge; nothing is lost
    return woken


def nudge_for_path(path: Path, state_dir: Path | None = None) -> int:
    """Wake the clients serving the checkout that contains ``path``.

    Resolves the root exactly as the client does (:func:`discover_repo_root`),
    so a hook and the client it wakes agree on the socket directory.
    """
    from .config import discover_repo_root

    try:
        start = path if path.is_dir() else path.parent
        return nudge_workspace(discover_repo_root(start), state_dir)
    except OSError:
        return 0


class AutoSync:
    """One session's background sync: a debounced worker plus a socket listener."""

    def __init__(
        self,
        sync: Callable[[], SyncResult],
        repo_root: Path,
        state_dir: Path,
        *,
        debounce_s: float = DEBOUNCE_S,
        idle_check_s: float = IDLE_CHECK_S,
        active_window_s: float = ACTIVE_WINDOW_S,
        listen: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sync = sync
        self._repo_root = repo_root
        self._state_dir = state_dir
        self._debounce_s = debounce_s
        self._idle_check_s = idle_check_s
        self._active_window_s = active_window_s
        self._listen = listen
        self._clock = clock
        self._cond = threading.Condition()
        self._pending_at: float | None = None
        self._last_check = 0.0
        self._last_activity = float("-inf")
        self._closed = False
        self._started = False
        self._busy_backoff_s = debounce_s
        self._worker: threading.Thread | None = None
        self._listener: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._socket_path: Path | None = None

    @property
    def socket_path(self) -> Path | None:
        return self._socket_path

    def start(self) -> None:
        """Idempotent. Call once the session has a View to sync."""
        with self._cond:
            if self._started or self._closed:
                return
            self._started = True
            self._last_check = self._clock()
        if self._listen:
            self._open_socket()
        self._worker = threading.Thread(target=self._run, name="lc-autosync", daemon=True)
        self._worker.start()

    def nudge(self) -> None:
        """Something may have changed the tree: check after the debounce."""
        self._schedule(self._debounce_s)

    def _schedule(self, delay_s: float) -> None:
        with self._cond:
            if self._pending_at is None:
                self._pending_at = self._clock() + delay_s
            self._cond.notify_all()

    def touch(self) -> None:
        """A tool call happened: keep the slow catch-all running."""
        with self._cond:
            self._last_activity = self._clock()
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        sock, path = self._socket, self._socket_path
        if sock is not None and path is not None:
            with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as waker:
                waker.sendto(_STOP, str(path))
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # wakes recv even if the stop was lost
        for thread in (self._listener, self._worker):
            if thread is not None:
                thread.join(timeout=5.0)
        if sock is not None:
            sock.close()
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()
            with contextlib.suppress(OSError):
                path.parent.rmdir()  # only when no other client serves this workspace

    # -- internals ------------------------------------------------------- #

    def _open_socket(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            return
        directory = socket_dir(self._repo_root, self._state_dir)
        path = directory / f"{os.getpid()}-{id(self) & 0xFFFFFF:x}.sock"
        if len(os.fsencode(path)) > _MAX_SOCKET_PATH:
            _LOG.debug("autosync: socket path %s is too long; hooks cannot reach this client", path)
            return
        if not _private_dir(directory):
            _LOG.debug("autosync: %s is not a private directory; hooks cannot reach this client", directory)
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            sock.bind(str(path))
            os.chmod(path, 0o600)
        except OSError:
            sock.close()
            return
        self._socket, self._socket_path = sock, path
        self._listener = threading.Thread(
            target=self._listen_loop, args=(sock,), name="lc-autosync-socket", daemon=True
        )
        self._listener.start()

    def _listen_loop(self, sock: socket.socket) -> None:
        while True:
            try:
                data = sock.recv(16)
            except OSError:
                return
            with self._cond:
                if self._closed:
                    return
            if data == _NUDGE:
                self.nudge()

    def _wait_s(self, now: float) -> float | None:
        """How long to sleep: until the nudge is due, the catch-all is due, or forever."""
        if self._pending_at is not None:
            return max(0.0, self._pending_at - now)
        if now - self._last_activity <= self._active_window_s:
            return max(0.0, self._last_check + self._idle_check_s - now)
        return None  # idle: no polling at all until a nudge or a call

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                self._cond.wait(self._wait_s(self._clock()))
                if self._closed:
                    return
                now = self._clock()
                due = False
                if self._pending_at is not None:
                    if now >= self._pending_at:
                        self._pending_at = None
                        due = True
                elif (
                    now - self._last_activity <= self._active_window_s and now - self._last_check >= self._idle_check_s
                ):
                    due = True
                if due:
                    self._last_check = now
            if not due:
                continue
            try:
                result = self._sync()
            except Exception:  # best effort: the next call's own check still holds
                _LOG.debug("autosync: background sync failed", exc_info=True)
                continue
            if result is None:
                # A call held the session and may have probed before this change.
                # Retry, backing off so a long call is not polled every half second.
                self._schedule(self._busy_backoff_s)
                self._busy_backoff_s = min(self._busy_backoff_s * 2, self._idle_check_s)
            else:
                self._busy_backoff_s = self._debounce_s
