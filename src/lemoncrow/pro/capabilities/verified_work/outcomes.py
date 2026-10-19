"""Bounded persistent outcome evidence for Verified Local Work model selection."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.models import RepairKind
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

_SCHEMA_VERSION = 1
_FILE_NAME = "verified_work_model_outcomes.json"
_MAX_MODELS = 64
_MAX_KINDS_PER_MODEL = 16
_MAX_RECENT_OUTCOMES = 100
# Repository confidence windows (G4-VLW): recent outcomes per repository,
# model and repair kind. Bounded by repository count (least recently updated
# evicted), models per repository and kinds per model; each window keeps the
# same _MAX_RECENT_OUTCOMES rows as the global window.
_MAX_REPOS = 64
_MAX_MODELS_PER_REPO = 16
_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ModelOutcomeStats:
    attempts: int = 0
    verified: int = 0
    promoted: int = 0
    total_latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_usd: float = 0.0
    shadow_attempts: int = 0
    enforce_attempts: int = 0
    shadow_verified: int = 0
    enforce_verified: int = 0
    last_at: str = ""

    @property
    def verification_rate(self) -> float:
        return self.verified / self.attempts if self.attempts > 0 else 0.0

    @property
    def average_latency_ms(self) -> float:
        return self.total_latency_ms / self.attempts if self.attempts > 0 else 0.0


def _root(root: Path | None = None) -> Path:
    return Path(root or active_store_root() or default_store_root()).expanduser()


def outcome_path(root: Path | None = None) -> Path:
    return _root(root) / _FILE_NAME


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _stats_from_dict(value: Any) -> ModelOutcomeStats:
    row = value if isinstance(value, dict) else {}
    return ModelOutcomeStats(
        attempts=_safe_int(row.get("attempts")),
        verified=_safe_int(row.get("verified")),
        promoted=_safe_int(row.get("promoted")),
        total_latency_ms=_safe_int(row.get("total_latency_ms")),
        input_tokens=_safe_int(row.get("input_tokens")),
        output_tokens=_safe_int(row.get("output_tokens")),
        provider_cost_usd=_safe_float(row.get("provider_cost_usd")),
        shadow_attempts=_safe_int(row.get("shadow_attempts")),
        enforce_attempts=_safe_int(row.get("enforce_attempts")),
        shadow_verified=_safe_int(row.get("shadow_verified")),
        enforce_verified=_safe_int(row.get("enforce_verified")),
        last_at=str(row.get("last_at") or "")[:64],
    )


def _stats_from_recent(value: Any, *, window: int) -> ModelOutcomeStats:
    rows = value if isinstance(value, list) else []
    selected = [row for row in rows[-max(1, window) :] if isinstance(row, dict)]
    if not selected:
        return ModelOutcomeStats()
    return ModelOutcomeStats(
        attempts=len(selected),
        verified=sum(int(bool(row.get("verified"))) for row in selected),
        promoted=sum(int(bool(row.get("promoted"))) for row in selected),
        total_latency_ms=sum(_safe_int(row.get("latency_ms")) for row in selected),
        input_tokens=sum(_safe_int(row.get("input_tokens")) for row in selected),
        output_tokens=sum(_safe_int(row.get("output_tokens")) for row in selected),
        provider_cost_usd=sum(_safe_float(row.get("provider_cost_usd")) for row in selected),
        shadow_attempts=sum(int(row.get("mode") == "shadow") for row in selected),
        enforce_attempts=sum(int(row.get("mode") == "enforce") for row in selected),
        shadow_verified=sum(int(row.get("mode") == "shadow" and bool(row.get("verified"))) for row in selected),
        enforce_verified=sum(int(row.get("mode") == "enforce" and bool(row.get("verified"))) for row in selected),
        last_at=str(selected[-1].get("at") or "")[:64],
    )


def load_recent_model_outcome(
    model: str,
    kind: RepairKind,
    *,
    root: Path | None = None,
    window: int = 20,
) -> ModelOutcomeStats:
    raw = _read(outcome_path(root))
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    model_row = models.get(model) if isinstance(models, dict) else None
    kinds = model_row.get("kinds") if isinstance(model_row, dict) else None
    kind_row = kinds.get(kind) if isinstance(kinds, dict) else None
    recent = kind_row.get("recent") if isinstance(kind_row, dict) else None
    return _stats_from_recent(recent, window=window)


def load_repository_outcome(
    model: str,
    kind: RepairKind,
    repo: str,
    *,
    root: Path | None = None,
    window: int = 20,
) -> ModelOutcomeStats:
    """Recent outcomes of ``model``/``kind`` recorded in one repository only.

    Pre-G4 rows carry no repository and never count toward any repository.
    """
    raw = _read(outcome_path(root))
    repos = raw.get("repos")
    repo_row = repos.get(repo) if isinstance(repos, dict) else None
    models = repo_row.get("models") if isinstance(repo_row, dict) else None
    model_row = models.get(model) if isinstance(models, dict) else None
    recent = model_row.get(kind) if isinstance(model_row, dict) else None
    return _stats_from_recent(recent, window=min(_MAX_RECENT_OUTCOMES, max(1, window)))


def qualification_model_outcome(
    model: str,
    kind: RepairKind,
    *,
    root: Path | None = None,
    min_samples: int = 20,
) -> ModelOutcomeStats:
    """Prefer recent evidence once it is large enough; otherwise use lifetime evidence."""
    recent = load_recent_model_outcome(model, kind, root=root, window=min(_MAX_RECENT_OUTCOMES, max(20, min_samples)))
    if recent.attempts >= min_samples:
        return recent
    return load_model_outcome(model, kind, root=root)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": _SCHEMA_VERSION, "models": {}, "repos": {}}
    if not isinstance(raw, dict):
        return {"schema_version": _SCHEMA_VERSION, "models": {}, "repos": {}}
    models = raw.get("models")
    if not isinstance(models, dict):
        models = {}
    repos = raw.get("repos")
    if not isinstance(repos, dict):
        repos = {}
    return {"schema_version": _SCHEMA_VERSION, "models": models, "repos": repos}


@contextlib.contextmanager
def _cross_process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _LOCK:
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt_api: Any = msvcrt
                msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt_unlock_api: Any = msvcrt
                    msvcrt_unlock_api.locking(handle.fileno(), msvcrt_unlock_api.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_model_outcome(model: str, kind: RepairKind, *, root: Path | None = None) -> ModelOutcomeStats:
    raw = _read(outcome_path(root))
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    model_row = models.get(model) if isinstance(models, dict) else None
    kinds = model_row.get("kinds") if isinstance(model_row, dict) else None
    return _stats_from_dict(kinds.get(kind) if isinstance(kinds, dict) else None)


def record_model_outcome(
    model: str,
    kind: RepairKind,
    *,
    verified: bool,
    promoted: bool,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    provider_cost_usd: float | None,
    mode: str = "shadow",
    root: Path | None = None,
    repo: str | None = None,
) -> ModelOutcomeStats:
    """Append one bounded aggregate outcome; no source/prompt content is stored.

    ``repo`` (a repository key) also appends the row to that repository's own
    confidence window, which Auto requires in addition to the global window.
    """
    path = outcome_path(root)
    with _cross_process_lock(path):
        raw = _read(path)
        models = raw.setdefault("models", {})
        if not isinstance(models, dict):
            models = {}
            raw["models"] = models
        if model not in models and len(models) >= _MAX_MODELS:
            # Evict the least-recently updated aggregate. Stable lexical tie-break.
            def _last(item: tuple[str, Any]) -> tuple[str, str]:
                name, value = item
                kinds = value.get("kinds") if isinstance(value, dict) else {}
                stamps = (
                    [str(v.get("last_at") or "") for v in kinds.values() if isinstance(v, dict)]
                    if isinstance(kinds, dict)
                    else []
                )
                return (max(stamps, default=""), name)

            oldest = min(models.items(), key=_last)[0]
            models.pop(oldest, None)
        model_row = models.setdefault(model, {"kinds": {}})
        if not isinstance(model_row, dict):
            model_row = {"kinds": {}}
            models[model] = model_row
        kinds = model_row.setdefault("kinds", {})
        if not isinstance(kinds, dict):
            kinds = {}
            model_row["kinds"] = kinds
        if kind not in kinds and len(kinds) >= _MAX_KINDS_PER_MODEL:
            oldest_kind = min(
                kinds,
                key=lambda key: (str((kinds.get(key) or {}).get("last_at") or ""), str(key)),
            )
            kinds.pop(oldest_kind, None)
        existing_kind = kinds.get(kind) if isinstance(kinds.get(kind), dict) else {}
        current = _stats_from_dict(existing_kind)
        recent = (
            list(existing_kind.get("recent") or [])
            if isinstance(existing_kind, dict) and isinstance(existing_kind.get("recent"), list)
            else []
        )
        now_iso = datetime.now(UTC).isoformat()
        row = {
            "verified": bool(verified),
            "promoted": bool(promoted),
            "latency_ms": max(0, int(latency_ms)),
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "provider_cost_usd": round(max(0.0, float(provider_cost_usd or 0.0)), 8),
            "mode": mode,
            "at": now_iso,
        }
        recent.append(row)
        recent = recent[-_MAX_RECENT_OUTCOMES:]
        if repo:
            _append_repository_row(raw, repo, model, kind, row, now_iso)
        updated = ModelOutcomeStats(
            attempts=current.attempts + 1,
            verified=current.verified + int(bool(verified)),
            promoted=current.promoted + int(bool(promoted)),
            total_latency_ms=current.total_latency_ms + max(0, int(latency_ms)),
            input_tokens=current.input_tokens + max(0, int(input_tokens)),
            output_tokens=current.output_tokens + max(0, int(output_tokens)),
            provider_cost_usd=current.provider_cost_usd + max(0.0, float(provider_cost_usd or 0.0)),
            shadow_attempts=current.shadow_attempts + int(mode == "shadow"),
            enforce_attempts=current.enforce_attempts + int(mode == "enforce"),
            shadow_verified=current.shadow_verified + int(mode == "shadow" and verified),
            enforce_verified=current.enforce_verified + int(mode == "enforce" and verified),
            last_at=now_iso,
        )
        kinds[kind] = {
            "attempts": updated.attempts,
            "verified": updated.verified,
            "promoted": updated.promoted,
            "total_latency_ms": updated.total_latency_ms,
            "input_tokens": updated.input_tokens,
            "output_tokens": updated.output_tokens,
            "provider_cost_usd": round(updated.provider_cost_usd, 8),
            "shadow_attempts": updated.shadow_attempts,
            "enforce_attempts": updated.enforce_attempts,
            "shadow_verified": updated.shadow_verified,
            "enforce_verified": updated.enforce_verified,
            "last_at": updated.last_at,
            "recent": recent,
        }
        _atomic_write(path, raw)
        return updated


def _append_repository_row(
    raw: dict[str, Any], repo: str, model: str, kind: str, row: dict[str, Any], now_iso: str
) -> None:
    repos = raw.get("repos")
    if not isinstance(repos, dict):
        repos = {}
        raw["repos"] = repos
    if repo not in repos and len(repos) >= _MAX_REPOS:
        oldest = min(
            repos,
            key=lambda key: (str((repos.get(key) or {}).get("last_at") or ""), str(key)),
        )
        repos.pop(oldest, None)
    repo_row = repos.get(repo)
    if not isinstance(repo_row, dict):
        repo_row = {}
        repos[repo] = repo_row
    models = repo_row.get("models")
    if not isinstance(models, dict):
        models = {}
        repo_row["models"] = models
    if model not in models and len(models) >= _MAX_MODELS_PER_REPO:
        stale = min(
            models,
            key=lambda key: (
                max(
                    (
                        str(rows[-1].get("at") or "")
                        for rows in (models.get(key) or {}).values()
                        if isinstance(rows, list) and rows and isinstance(rows[-1], dict)
                    ),
                    default="",
                ),
                str(key),
            ),
        )
        models.pop(stale, None)
    kinds = models.get(model)
    if not isinstance(kinds, dict):
        kinds = {}
        models[model] = kinds
    if kind not in kinds and len(kinds) >= _MAX_KINDS_PER_MODEL:
        kinds.pop(sorted(kinds)[0], None)
    rows = kinds.get(kind)
    window = list(rows) if isinstance(rows, list) else []
    window.append(dict(row))
    kinds[kind] = window[-_MAX_RECENT_OUTCOMES:]
    repo_row["last_at"] = now_iso


def load_all_model_outcomes(*, root: Path | None = None) -> dict[str, dict[str, ModelOutcomeStats]]:
    raw = _read(outcome_path(root))
    raw_models = raw.get("models")
    models: dict[str, Any] = raw_models if isinstance(raw_models, dict) else {}
    result: dict[str, dict[str, ModelOutcomeStats]] = {}
    for model, value in models.items():
        if not isinstance(value, dict):
            continue
        kinds = value.get("kinds")
        if not isinstance(kinds, dict):
            continue
        result[str(model)] = {str(kind): _stats_from_dict(stats) for kind, stats in kinds.items()}
    return result


def summarize_outcomes(*, root: Path | None = None) -> dict[str, Any]:
    models = load_all_model_outcomes(root=root)
    totals = {
        "attempts": 0,
        "verified": 0,
        "promoted": 0,
        "shadow_attempts": 0,
        "enforce_attempts": 0,
        "shadow_verified": 0,
        "enforce_verified": 0,
        "total_latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_cost_usd": 0.0,
    }
    by_kind: dict[str, dict[str, Any]] = {}
    for model_kinds in models.values():
        for kind, stats in model_kinds.items():
            row = by_kind.setdefault(
                kind,
                {"attempts": 0, "verified": 0, "promoted": 0, "total_latency_ms": 0},
            )
            for key in (
                "attempts",
                "verified",
                "promoted",
                "shadow_attempts",
                "enforce_attempts",
                "shadow_verified",
                "enforce_verified",
                "total_latency_ms",
                "input_tokens",
                "output_tokens",
            ):
                totals[key] += int(getattr(stats, key))
            totals["provider_cost_usd"] += stats.provider_cost_usd
            row["attempts"] += stats.attempts
            row["verified"] += stats.verified
            row["promoted"] += stats.promoted
            row["total_latency_ms"] += stats.total_latency_ms
    attempts = int(totals["attempts"])
    totals["provider_cost_usd"] = round(float(totals["provider_cost_usd"]), 8)
    totals["verification_rate"] = float(totals["verified"]) / attempts if attempts else 0.0
    totals["average_latency_ms"] = float(totals["total_latency_ms"]) / attempts if attempts else 0.0
    for row in by_kind.values():
        count = int(row["attempts"])
        row["verification_rate"] = float(row["verified"]) / count if count else 0.0
        row["average_latency_ms"] = float(row["total_latency_ms"]) / count if count else 0.0
    return {
        "schema_version": 1,
        "totals": totals,
        "by_kind": dict(sorted(by_kind.items())),
        "model_count": len(models),
        "estimated_frontier_savings": None,
        "savings_status": "not_qualified_without_paired_baseline",
    }


__all__ = [
    "ModelOutcomeStats",
    "load_all_model_outcomes",
    "load_model_outcome",
    "load_recent_model_outcome",
    "load_repository_outcome",
    "outcome_path",
    "qualification_model_outcome",
    "record_model_outcome",
    "summarize_outcomes",
]
