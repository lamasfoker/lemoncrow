"""Rebuildable cross-process lease for the currently synced authoring View.

The long-lived MCP/plugin client owns the source View in memory, but ``lcr`` is a
separate process.  This tiny record lets Review adopt that already-synced View
without walking the checkout again.  It contains no source bytes, credentials,
or index data; an exact workspace fingerprint is required before reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LiveViewBinding:
    server_url: str
    repo_id: str
    view_id: str
    view_revision: int
    workspace_fingerprint: str


def _binding_path(state_dir: Path, repo_root: Path) -> Path:
    canonical = str(repo_root.expanduser().resolve()).encode("utf-8", errors="surrogateescape")
    key = hashlib.sha256(canonical).hexdigest()[:32]
    return state_dir / "live-views" / f"{key}.json"


def write_live_view_binding(
    state_dir: Path,
    repo_root: Path,
    binding: LiveViewBinding,
) -> None:
    if not (binding.server_url and binding.repo_id and binding.view_id and binding.workspace_fingerprint):
        return
    target = _binding_path(state_dir, repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "server_url": binding.server_url.rstrip("/"),
        "repo_id": binding.repo_id,
        "view_id": binding.view_id,
        "view_revision": binding.view_revision,
        "workspace_fingerprint": binding.workspace_fingerprint,
    }
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_live_view_binding(
    state_dir: Path,
    repo_root: Path,
    *,
    server_url: str,
) -> LiveViewBinding | None:
    target = _binding_path(state_dir, repo_root)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        binding = LiveViewBinding(
            server_url=str(raw.get("server_url") or "").rstrip("/"),
            repo_id=str(raw.get("repo_id") or ""),
            view_id=str(raw.get("view_id") or ""),
            view_revision=int(raw.get("view_revision") or 0),
            workspace_fingerprint=str(raw.get("workspace_fingerprint") or ""),
        )
    except (TypeError, ValueError):
        return None
    if binding.server_url != server_url.rstrip("/"):
        return None
    if not binding.repo_id or not binding.view_id or binding.view_revision < 0 or not binding.workspace_fingerprint:
        return None
    return binding


def clear_live_view_binding(
    state_dir: Path,
    repo_root: Path,
    *,
    expected_view_id: str = "",
) -> None:
    target = _binding_path(state_dir, repo_root)
    if expected_view_id:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(raw, dict) or str(raw.get("view_id") or "") != expected_view_id:
            return
    target.unlink(missing_ok=True)


__all__ = [
    "LiveViewBinding",
    "clear_live_view_binding",
    "load_live_view_binding",
    "write_live_view_binding",
]
