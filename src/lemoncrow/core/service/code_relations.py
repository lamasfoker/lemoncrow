"""Plausibility rules shared by every surface that draws code relationships.

`/code` and Review read the same current-index link layer, and both must drop
the same name collisions, or the two surfaces disagree about one relationship.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

# Name-resolved call/reference edges are only meaningful inside one language
# ecosystem. The current-index graph resolves by name, so a Python ``bool()``
# or ``round()`` call can land on a TypeScript symbol that happens to share the
# name. Imports are path-resolved by the parser and are trusted as-is.
LANGUAGE_FAMILIES = {
    ".py": "python",
    ".pyi": "python",
    ".pyx": "python",
    ".ts": "js",
    ".tsx": "js",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".mts": "js",
    ".cts": "js",
    ".vue": "js",
    ".svelte": "js",
    ".css": "css",
    ".scss": "css",
    ".sass": "css",
    ".less": "css",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".go": "go",
    ".rs": "rust",
    ".java": "jvm",
    ".kt": "jvm",
    ".kts": "jvm",
    ".scala": "jvm",
    ".groovy": "jvm",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".hpp": "c",
    ".hh": "c",
    ".m": "c",
    ".mm": "c",
    ".cs": "dotnet",
    ".fs": "dotnet",
    ".vb": "dotnet",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".ex": "elixir",
    ".exs": "elixir",
    ".lua": "lua",
    ".sql": "sql",
}


def language_family(path: str) -> str:
    return LANGUAGE_FAMILIES.get(PurePosixPath(path).suffix.lower(), "")


def file_category(path: str) -> str:
    try:
        from lemoncrow.pro.capabilities.review.gitdiff import classify_path

        return str(classify_path(path))
    except Exception:
        return "production"


def implausible_relation(
    source_path: str,
    target_path: str,
    kind: str,
    categories: Mapping[str, str] | None = None,
) -> str:
    """Why a current-index file relationship is a name collision, or ``""``.

    Callers record the reason (as a count) rather than silently erasing it, so
    a surface can say how much surrounding noise it declined to draw.
    """

    if kind not in {"calls", "references"}:
        return ""
    source_family = language_family(source_path)
    target_family = language_family(target_path)
    if not source_family or source_family != target_family:
        return "cross_language"
    known = categories or {}
    source_category = known.get(source_path) or file_category(source_path)
    target_category = known.get(target_path) or file_category(target_path)
    if source_category != "test" and target_category == "test":
        # Shipping code does not call into test modules; a same-named test helper
        # is the only way the index produces this edge.
        return "production_to_test"
    return ""


_RELATION_PRIORITY = {"calls": 0, "imports": 1, "references": 2}


def balanced_relationship_rows(
    rows: list[dict[str, Any]],
    seeds: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Strongest file relationships, split fairly between two questions.

    Links among the seed paths say how the changed files relate; links out to
    other files say what surrounding code is involved. A large change has
    enough internal links to fill any limit on their own, so each side is
    guaranteed half and lends the other whatever it does not use.
    """

    def strength(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
        return (
            _RELATION_PRIORITY.get(str(row["kind"]), 3),
            -int(row["weight"]),
            str(row["source_path"]),
            str(row["target_path"]),
        )

    internal = sorted(
        (row for row in rows if row["source_path"] in seeds and row["target_path"] in seeds),
        key=strength,
    )
    external = sorted(
        (row for row in rows if not (row["source_path"] in seeds and row["target_path"] in seeds)),
        key=strength,
    )
    internal_share = min(len(internal), max(limit // 2, limit - len(external)))
    return internal[:internal_share] + external[: max(0, limit - internal_share)]


__all__ = [
    "LANGUAGE_FAMILIES",
    "balanced_relationship_rows",
    "file_category",
    "implausible_relation",
    "language_family",
]
