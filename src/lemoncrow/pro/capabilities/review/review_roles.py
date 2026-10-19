"""Deterministic Review-role classification for changed material.

Roles are presentation metadata over exact Review targets, never a second scope or
judgment model. Repository-owned .gitattributes at the trusted base wins when
available; otherwise a small deterministic path classifier is used. No model call
participates in classification.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from lemoncrow.pro.capabilities.review.targets import ReviewTarget

ReviewRole = Literal[
    "implementation",
    "tests",
    "documentation",
    "generated",
    "agent_guidance",
    "localization",
    "assets",
    "other",
]
ReviewRoleSource = Literal["gitattributes", "inferred"]

ROLE_VALUES: tuple[ReviewRole, ...] = (
    "implementation",
    "tests",
    "documentation",
    "generated",
    "agent_guidance",
    "localization",
    "assets",
    "other",
)
SUPPORTING_ROLES: frozenset[ReviewRole] = frozenset(
    {"tests", "documentation", "generated", "agent_guidance", "localization", "assets"}
)
_EXPLICIT_ATTRS = ("lemoncrow-review-role", "review-role", "linguist-generated")
_ASSET_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".wav",
        ".mp4",
        ".mov",
        ".pdf",
    }
)
_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc"})
_LOCALIZATION_SUFFIXES = frozenset({".po", ".pot", ".mo", ".xlf", ".xliff"})


@dataclass(frozen=True)
class FileRole:
    path: str
    role: ReviewRole
    source: ReviewRoleSource
    target_ids: tuple[str, ...]
    additions: int
    deletions: int
    collapsed_default: bool

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "source": self.source,
            "target_ids": list(self.target_ids),
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_lines": self.changed_lines,
            "collapsed_default": self.collapsed_default,
        }


def _normalize_explicit_role(value: str) -> ReviewRole | None:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "test": "tests",
        "docs": "documentation",
        "doc": "documentation",
        "agent": "agent_guidance",
        "guidance": "agent_guidance",
        "i18n": "localization",
        "l10n": "localization",
        "asset": "assets",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in ROLE_VALUES else None  # type: ignore[return-value]


def infer_review_role(path: str) -> ReviewRole:
    """Classify a path without repository policy.

    The fallback is intentionally boring and stable. Ambiguous source/configuration
    remains implementation rather than being hidden as support material.
    """

    normalized = path.replace("\\", "/").lstrip("./")
    lower = normalized.lower()
    pure = PurePosixPath(lower)
    parts = pure.parts
    name = pure.name
    suffix = pure.suffix

    if (
        name in {"agents.md", "claude.md", "copilot-instructions.md"}
        or lower.startswith(".cursor/rules/")
        or lower.startswith(".github/instructions/")
        or lower.startswith(".github/prompts/")
    ):
        return "agent_guidance"
    if (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    ):
        return "tests"
    if (
        "locales" in parts
        or "locale" in parts
        or "i18n" in parts
        or "l10n" in parts
        or suffix in _LOCALIZATION_SUFFIXES
    ):
        return "localization"
    if (
        "generated" in parts
        or "vendor" in parts
        or "third_party" in parts
        or "dist" in parts
        or name.endswith(".min.js")
        or name.endswith(".min.css")
        or name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock"}
    ):
        return "generated"
    if "docs" in parts or suffix in _DOC_SUFFIXES or name.startswith("readme"):
        return "documentation"
    if suffix in _ASSET_SUFFIXES or "assets" in parts or "images" in parts:
        return "assets"
    if name in {".gitignore", ".dockerignore", ".editorconfig"}:
        return "other"
    return "implementation"


def _trusted_base_attributes(
    repo_root: str, base_sha: str, paths: Sequence[str]
) -> tuple[dict[str, dict[str, str]], bool]:
    """Resolve only trusted-base attributes for paths using Git's own matcher.

    The boolean says whether the trusted base was readable, so "no matching
    attribute" remains distinct from "classification policy unavailable".
    """

    if not repo_root or not base_sha or not paths:
        return {}, False
    root = Path(repo_root)
    if not root.is_dir():
        return {}, False

    try:
        with tempfile.TemporaryDirectory(prefix="lc-review-attrs-") as temp_dir:
            index_path = str(Path(temp_dir) / "index")
            env = {**os.environ, "GIT_INDEX_FILE": index_path}
            read_tree = subprocess.run(
                ["git", "-C", str(root), "read-tree", base_sha],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if read_tree.returncode != 0:
                return {}, False
            checked = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "check-attr",
                    "--cached",
                    "-z",
                    *_EXPLICIT_ATTRS,
                    "--",
                    *paths,
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if checked.returncode != 0:
                return {}, False
    except (OSError, subprocess.SubprocessError):
        return {}, False

    fields = checked.stdout.decode("utf-8", errors="replace").split("\0")
    attributes: dict[str, dict[str, str]] = {}
    for index in range(0, len(fields) - 2, 3):
        path, key, value = fields[index : index + 3]
        if path and key and value not in {"unspecified", "unset"}:
            attributes.setdefault(path, {})[key] = value
    return attributes, True


def _role_for(path: str, attrs: Mapping[str, str]) -> tuple[ReviewRole, ReviewRoleSource]:
    for key in ("lemoncrow-review-role", "review-role"):
        role = _normalize_explicit_role(attrs.get(key, ""))
        if role is not None:
            return role, "gitattributes"
    if attrs.get("linguist-generated") == "set":
        return "generated", "gitattributes"
    return infer_review_role(path), "inferred"


def review_material_projection(
    targets: Sequence[ReviewTarget],
    *,
    repo_root: str,
    base_sha: str,
) -> dict[str, Any]:
    """Project file roles and implementation-vs-total size over exact targets."""

    by_path: dict[str, list[ReviewTarget]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    paths = tuple(by_path)
    attributes, trusted_base_known = _trusted_base_attributes(repo_root, base_sha, paths)

    files: list[FileRole] = []
    for path, scoped in by_path.items():
        role, source = _role_for(path, attributes.get(path, {}))
        additions = sum(max(0, target.additions) for target in scoped)
        deletions = sum(max(0, target.deletions) for target in scoped)
        requires_attention = any(
            target.verification.fail_count > 0
            or target.verification.unknown_count > 0
            or target.attention_level == "high"
            or (target.priority.outstanding and target.priority.tier <= 1)
            for target in scoped
        )
        files.append(
            FileRole(
                path=path,
                role=role,
                source=source,
                target_ids=tuple(target.target_id for target in scoped),
                additions=additions,
                deletions=deletions,
                collapsed_default=role in SUPPORTING_ROLES and not requires_attention,
            )
        )

    total_lines = sum(file.changed_lines for file in files)
    implementation_lines = sum(file.changed_lines for file in files if file.role == "implementation")
    role_counts = {
        role: {
            "files": sum(1 for file in files if file.role == role),
            "changed_lines": sum(file.changed_lines for file in files if file.role == role),
        }
        for role in ROLE_VALUES
        if any(file.role == role for file in files)
    }
    return {
        "files": [file.payload() for file in files],
        "roles": role_counts,
        "implementation_changed_lines": implementation_lines,
        "total_changed_lines": total_lines,
        "explicit_metadata_state": "present" if attributes else ("absent" if trusted_base_known else "unavailable"),
    }
