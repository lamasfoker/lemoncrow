"""Shared resolution of the managed LemonCode/OpenCode host binary.

Installation/update policy belongs to the Runtime CLI. Review only needs to
locate an already-installed host executable, so that lookup lives in Core.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def managed_host_binary_path(store_root: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return store_root / "bin" / f"lemoncode-host{suffix}"


def _source_host_binary() -> Path | None:
    os_name = {"linux": "linux", "darwin": "darwin"}.get(platform.system().lower())
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine().lower())
    if os_name is None or arch is None:
        return None
    repository = Path(__file__).resolve().parents[4]
    candidate = repository / "lemoncode" / "packages" / "opencode" / "dist"
    candidate = candidate / f"lemoncode-{os_name}-{arch}" / "bin" / "lemoncode"
    return candidate if _is_executable(candidate) else None


def resolve_managed_host_binary(store_root: Path) -> str | None:
    """Locate an existing managed host without installing or downloading it."""

    override = os.environ.get("LEMONCODE_HOST_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not _is_executable(candidate):
            raise ValueError(f"LEMONCODE_HOST_BIN is not executable: {candidate}")
        return str(candidate.resolve())
    installed = managed_host_binary_path(store_root)
    if _is_executable(installed):
        return str(installed)
    source = _source_host_binary()
    if source is not None:
        return str(source)
    return shutil.which("lemoncode-host")


__all__ = ["managed_host_binary_path", "resolve_managed_host_binary"]
