"""Shared location policy for ephemeral LemonCrow spill artifacts.

Spills are recoverable copies of oversized tool output, not durable application
state. Keep them out of ``~/.lemoncrow`` by default while allowing every local
LemonCrow process for the same user to resolve the same directory.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

_SPILL_DIR_ENV = "LEMONCROW_MCP_SPILL_DIR"


def _ensure_private_directory(path: Path, *, validate_owner: bool = False) -> Path:
    """Create *path* as an owner-only directory and return its resolved path.

    ``validate_owner`` is used for the predictable directory created under a
    shared system temp root. Refusing a pre-created symlink or foreign-owned
    directory is safer than spilling potentially sensitive tool output there.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if validate_owner and hasattr(os, "getuid"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"unsafe LemonCrow spill directory: {path}")
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path.resolve()


def resolve_spill_dir() -> Path:
    """Return the shared ephemeral spill directory for this user.

    Resolution order:

    1. ``LEMONCROW_MCP_SPILL_DIR`` when explicitly configured.
    2. ``$XDG_RUNTIME_DIR/lemoncrow/spill`` on systems that provide it.
    3. A user-scoped directory below the OS temp directory. On POSIX this is
       ``<temp>/lemoncrow-<uid>/spill``; elsewhere the OS temp directory is
       already normally user-scoped, so ``<temp>/lemoncrow/spill`` is used.
    """
    configured = os.environ.get(_SPILL_DIR_ENV, "").strip()
    if configured:
        return _ensure_private_directory(Path(configured).expanduser())

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    runtime_root = Path(runtime_dir).expanduser() if runtime_dir else None
    if runtime_root is not None and runtime_root.is_absolute():
        # An inherited runtime dir (``sudo -E``, a container without one) may
        # not be ours to write; the user-scoped temp root below still is.
        with contextlib.suppress(OSError):
            return _ensure_private_directory(runtime_root / "lemoncrow" / "spill")

    temp_root = Path(tempfile.gettempdir())
    if hasattr(os, "getuid"):
        user_root = _ensure_private_directory(temp_root / f"lemoncrow-{os.getuid()}", validate_owner=True)
    else:
        user_root = _ensure_private_directory(temp_root / "lemoncrow")
    return _ensure_private_directory(user_root / "spill")
