"""Pi v3 JSONL session importer with deterministic active-branch selection."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.gateway.hosts.session_parsers._common import record_normalized_session
from lemoncrow.infra.storage.bundle import StoreBundle

logger = logging.getLogger(__name__)

_KNOWN_ENTRY_TYPES = {
    "session",
    "message",
    "model_change",
    "thinking_level_change",
    "compaction",
    "branch_summary",
    "custom",
    "custom_message",
    "label",
    "session_info",
}


def _candidate_roots(store_root: Path | None) -> list[Path]:
    roots: list[Path] = []
    explicit_session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if explicit_session_dir:
        roots.append(Path(explicit_session_dir).expanduser())
    else:
        config_dir = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
        roots.append((Path(config_dir).expanduser() if config_dir else Path.home() / ".pi" / "agent") / "sessions")
    configured_store = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    managed_root = Path(configured_store).expanduser() if configured_store else Path.home() / ".lemoncrow"
    roots.append(managed_root / "hosts" / "pi" / "sessions")
    if store_root is not None:
        roots.append(Path(store_root).expanduser() / "hosts" / "pi" / "sessions")
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        normalized = root.resolve(strict=False)
        if normalized not in seen:
            seen.add(normalized)
            result.append(root)
    return result


def find_pi_sessions(root: Path | None = None, *, store_root: Path | None = None) -> Iterator[Path]:
    roots = [Path(root).expanduser()] if root is not None else _candidate_roots(store_root)
    seen: set[Path] = set()
    for candidate in roots:
        if candidate.is_file() and candidate.suffix == ".jsonl":
            paths: Iterator[Path] = iter((candidate,))
        elif candidate.is_dir():
            paths = candidate.rglob("*.jsonl")
        else:
            continue
        for path in paths:
            resolved = path.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def _parse_lines(raw_content: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_line in raw_content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def pi_session_id(raw_content: str, fallback: str) -> str:
    for entry in _parse_lines(raw_content):
        if entry.get("type") == "session":
            session_id = str(entry.get("id") or "").strip()
            if session_id:
                return session_id
            break
    return fallback


def select_pi_active_branch(raw_content: str) -> str:
    """Return header + entries on the current leaf's parent chain.

    Pi persists all branches in one JSONL file. The current leaf is the final
    tree entry in append order; walking ``parentId`` back to null reproduces the
    active path deterministically without throwing away the original artifact.
    """
    entries = _parse_lines(raw_content)
    headers = [entry for entry in entries if entry.get("type") == "session"]
    tree_entries = [entry for entry in entries if str(entry.get("id") or "").strip()]
    if not tree_entries:
        return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in headers)

    by_id = {str(entry["id"]): entry for entry in tree_entries}
    current = str(tree_entries[-1]["id"])
    active_ids: set[str] = set()
    while current and current not in active_ids:
        entry = by_id.get(current)
        if entry is None:
            break
        active_ids.add(current)
        parent = entry.get("parentId")
        current = str(parent) if parent is not None else ""

    selected = [*headers, *(entry for entry in tree_entries if str(entry["id"]) in active_ids)]
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in selected)


def _session_name(active_content: str) -> str | None:
    name: str | None = None
    for entry in _parse_lines(active_content):
        if entry.get("type") == "session_info":
            value = str(entry.get("name") or "").strip()
            if value:
                name = value
    return name


def _warn_unknown_entries(path: Path, raw_content: str) -> None:
    unknown = sorted(
        {
            str(entry.get("type"))
            for entry in _parse_lines(raw_content)
            if entry.get("type") and entry.get("type") not in _KNOWN_ENTRY_TYPES
        }
    )
    if unknown:
        logger.warning("pi: preserving unknown session entries in %s: %s", path.name, ", ".join(unknown))


def _managed_usage_paths(store_root: Path, session_id: str) -> list[Path]:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)
    roots = [Path(store_root)]
    configured = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if configured:
        roots.append(Path(configured).expanduser())
    roots.append(Path.home() / ".lemoncrow")
    paths: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        path = root / f"pi-managed-usage-{safe}.json"
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _managed_usage_event(store_root: Path, session_id: str) -> dict[str, Any] | None:
    """Project the managed-Pi gateway sidecar into one normalized usage event.

    Managed Pi is only a carrier: its own provider advertises zero prices and
    persists zero usage, while LemonCrow's gateway owns the real model calls.
    The extension records the gateway cumulative delta under this Pi session id.
    Keep the usage measured but the dollar total explicitly *priced*, not billed.
    """
    raw: dict[str, Any] | None = None
    for path in _managed_usage_paths(store_root, session_id):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("schema_version") != 1 or candidate.get("source") != "pi_managed_gateway":
            continue
        if str(candidate.get("session_id") or "") != session_id:
            continue
        raw = candidate
        break
    if raw is None:
        return None
    totals = raw.get("totals")
    if not isinstance(totals, dict):
        return None

    def _int(key: str) -> int:
        try:
            return max(0, int(totals.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    cache_read = _int("cache_read_tokens")
    cache_write = _int("cache_write_tokens")
    if not any((input_tokens, output_tokens, cache_read, cache_write)):
        return None
    try:
        priced_cost = max(0.0, float(totals.get("cost_usd") or 0.0))
    except (TypeError, ValueError):
        priced_cost = 0.0
    updated_ms = raw.get("updated_at_ms")
    try:
        timestamp = (
            datetime.fromtimestamp(float(updated_ms) / 1000.0, tz=UTC).isoformat()
            if updated_ms is not None and not isinstance(updated_ms, bool)
            else datetime.now(UTC).isoformat()
        )
    except (TypeError, ValueError, OSError):
        timestamp = datetime.now(UTC).isoformat()
    return {
        "type": "message",
        "id": f"lemoncrow-managed-usage-{session_id}",
        "timestamp": timestamp,
        "data": {
            "synthetic": True,
            "role": "assistant",
            "provider": str(raw.get("provider") or "lemoncrow"),
            "model": str(raw.get("model") or ""),
            "content": [],
            "usageMeasurementSource": "lemoncrow_gateway",
            "pricedCostUsd": priced_cost,
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
            },
        },
    }


class PiImporter:
    """Import Pi JSONL while retaining the complete raw branching artifact."""

    def __init__(self, store: StoreBundle) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        stamped: list[tuple[float, Path]] = []
        for path in find_pi_sessions(root, store_root=self.store.root):
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(stamped, key=lambda item: item[0], reverse=True)]
        if limit is not None:
            paths = paths[:limit]
        imported: list[str] = []
        for path in paths:
            try:
                trace_id = self.import_session(path, force=force)
            except Exception:
                logger.exception("skipping Pi session %s", path)
                continue
            if trace_id:
                imported.append(trace_id)
        return imported

    def import_session(self, jsonl_path: Path, *, force: bool = False) -> str | None:
        raw_content = jsonl_path.read_text(encoding="utf-8", errors="replace")
        _warn_unknown_entries(jsonl_path, raw_content)
        session_id = pi_session_id(raw_content, jsonl_path.stem)
        active_content = select_pi_active_branch(raw_content)
        managed_usage = _managed_usage_event(self.store.root, session_id)
        if managed_usage is not None:
            serialized_usage = json.dumps(managed_usage, ensure_ascii=False)
            active_content = f"{active_content}\n{serialized_usage}" if active_content else serialized_usage
        source_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="pi",
            session_id=session_id,
            relative_path=jsonl_path.name,
            content_path=f"raw/pi/{jsonl_path.name}",
            raw_content=raw_content,
            trace_content=active_content,
            source_mtime=source_mtime,
            source_path=str(jsonl_path),
            force=force,
            task=_session_name(active_content),
        )


__all__ = [
    "PiImporter",
    "find_pi_sessions",
    "pi_session_id",
    "select_pi_active_branch",
]
