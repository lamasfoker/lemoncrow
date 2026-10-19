"""Repository-local paths and same-host proof shared by Review and Runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

LOCAL_FS_PROOF_PATH: Final[str] = ".lemoncrow-local-fs-proof"
REVIEW_CONFIG_PATH: Final[str] = ".lemoncrow/review.yaml"

__all__ = ["LOCAL_FS_PROOF_PATH", "REVIEW_CONFIG_PATH", "write_local_fs_proof"]


def git_metadata_dir(repo_root: Path) -> Path | None:
    """Return this checkout's Git metadata directory without invoking Git."""
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    prefix = "gitdir:"
    if not first.lower().startswith(prefix):
        return None
    raw = first[len(prefix) :].strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def exclude_local_fs_proof(repo_root: Path) -> None:
    git_dir = git_metadata_dir(repo_root)
    if git_dir is None:
        return
    target = git_dir / "info" / "exclude"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    rule = f"/{LOCAL_FS_PROOF_PATH}"
    if any(line.strip() == rule for line in existing.splitlines()):
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}# LemonCrow same-host capability proof\n{rule}\n")


def write_local_fs_proof(repo_root: Path, nonce: str) -> bool:
    """Keep the proof outside Git status and source manifests."""
    try:
        exclude_local_fs_proof(repo_root)
        # Replace only the proof *path*, never follow a pre-existing symlink
        # (or hardlink) into another file. O_EXCL prevents a rename/recreate
        # race from turning the proof into a repository-controlled file write.
        proof = repo_root / LOCAL_FS_PROOF_PATH
        proof.unlink(missing_ok=True)
        descriptor = os.open(proof, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(nonce)
    except OSError:
        return False
    return True
