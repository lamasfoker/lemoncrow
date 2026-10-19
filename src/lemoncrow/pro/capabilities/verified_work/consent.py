"""Repository-scoped Auto consent and repository confidence for Verified Local Work.

Auto (``verified_work.mode = enforce``) is a request, not consent. A verified
candidate is promoted into a live checkout only when all of these hold:

* the host adapter is qualified for transparent repair (``AUTO_HOSTS``);
* the checkout belongs to a git repository whose owner explicitly opted in
  with ``lc rt savings auto`` (recorded with who, when and how);
* that opt-in covers the exact (model, repair kind) pair -- a model or kind
  that graduates later is never added implicitly;
* the repository's *own* recent outcome window for that pair qualifies.

Migration rule (pre-G4 Auto users): a stored ``verified_work.mode = enforce``
without a grant is treated as Learning in every repository. The first eligible
verifier failure in such a repository carries one host-visible notice asking
for the opt-in (recorded per repository so it is asked once), and
``lc rt savings status`` keeps showing the pending opt-in. Nothing is
grandfathered and pre-G4 outcomes carry no repository, so each repository
rebuilds its own window before Auto can apply there.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.models import RepairKind
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

from .outcomes import ModelOutcomeStats, _atomic_write, _cross_process_lock, load_repository_outcome

_SCHEMA_VERSION = 1
_FILE_NAME = "verified_work_consent.json"
# Bounds for the consent ledger. Entries without an active grant (notice-only)
# are evicted before grants; per-repository history keeps the newest events.
MAX_REPOSITORIES = 256
MAX_HISTORY = 20

# Host adapters qualified for transparent promotion. Claude Code is covered by
# the native-Bash Mod bridge and the full-Runtime ``lc`` bash tool; ``cli`` is
# the explicit ``lc rt work repair`` command. Codex and OpenCode have no
# post-tool adapter (their default ``lite-mcp`` bash has no repair hook), so
# they stay Learning-only even when a full-Runtime MCP bash could repair.
AUTO_HOSTS: frozenset[str] = frozenset({"claude", "cli"})
HOST_SUPPORT: dict[str, str] = {"claude": "supported", "codex": "unsupported", "opencode": "unsupported"}

CONSENT_NOTICE = (
    "[lc] Local Savings Auto is paused in this repository: Auto now needs a per-repository opt-in. "
    "Run `lc rt savings auto` here to opt in, or `lc rt savings learn` to keep Learning only. "
    "(asked once; `lc rt savings status` shows it)"
)


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """One git repository; worktrees of the same repository share ``key``."""

    key: str
    root: str
    common_dir: str


@dataclass(frozen=True, slots=True)
class AutoGrant:
    repo_key: str
    repo_root: str
    granted_at: str
    granted_by: str
    via: str
    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AutoGate:
    allowed: bool
    reason: str
    repo: RepositoryIdentity | None = None
    models: tuple[str, ...] = ()


def _root(root: Path | None = None) -> Path:
    return Path(root or active_store_root() or default_store_root()).expanduser()


def consent_path(root: Path | None = None) -> Path:
    return _root(root) / _FILE_NAME


def _now() -> str:
    return datetime.now(UTC).isoformat()


def local_actor() -> str:
    """The local account recording a consent event (never read from env vars)."""
    try:
        import pwd

        name = pwd.getpwuid(os.getuid()).pw_name
        if name:
            return str(name)
    except (ImportError, KeyError, OSError, AttributeError):
        pass
    try:
        return os.getlogin()
    except OSError:
        return "unknown"


def repository_identity(path: Path | str) -> RepositoryIdentity | None:
    """Resolve the repository a path belongs to, keyed by its shared git dir."""
    base = Path(path).expanduser()
    if not base.is_dir():
        base = base.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(lines) < 2:
        return None
    toplevel = Path(lines[0]).resolve()
    common = Path(lines[1])
    if not common.is_absolute():
        common = base / common
    common = common.resolve()
    key = hashlib.sha256(str(common).encode("utf-8", errors="surrogateescape")).hexdigest()[:16]
    return RepositoryIdentity(key=key, root=str(toplevel), common_dir=str(common))


def _read(path: Path) -> dict[str, Any]:
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    repos = raw.get("repositories") if isinstance(raw, dict) else None
    return {"schema_version": _SCHEMA_VERSION, "repositories": repos if isinstance(repos, dict) else {}}


def _pairs(value: Any) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, list | tuple) and len(item) == 2 and all(isinstance(part, str) for part in item):
                pair = (str(item[0]), str(item[1]))
                if pair not in rows:
                    rows.append(pair)
    return tuple(rows)


def _grant_from(repo_key: str, entry: Any) -> AutoGrant | None:
    if not isinstance(entry, dict):
        return None
    grant = entry.get("grant")
    if not isinstance(grant, dict):
        return None
    pairs = _pairs(grant.get("pairs"))
    if not pairs:
        return None
    return AutoGrant(
        repo_key=repo_key,
        repo_root=str(entry.get("repo_root") or ""),
        granted_at=str(grant.get("granted_at") or ""),
        granted_by=str(grant.get("granted_by") or ""),
        via=str(grant.get("via") or ""),
        pairs=pairs,
    )


def _entry(repos: dict[str, Any], repo_key: str, *, repo_root: str = "", common_dir: str = "") -> dict[str, Any]:
    entry = repos.get(repo_key)
    if not isinstance(entry, dict):
        entry = {}
        repos[repo_key] = entry
    if repo_root:
        entry["repo_root"] = repo_root
    if common_dir:
        entry["common_dir"] = common_dir
    entry["updated_at"] = _now()
    return entry


def _bound(repos: dict[str, Any]) -> None:
    """Keep at most MAX_REPOSITORIES entries; evict notice-only entries first."""
    while len(repos) > MAX_REPOSITORIES:
        _, victim = min(
            enumerate(repos),
            key=lambda item: (
                isinstance((repos.get(item[1]) or {}).get("grant"), dict),
                str((repos.get(item[1]) or {}).get("updated_at") or ""),
                item[0],
            ),
        )
        repos.pop(victim, None)


def _history(entry: dict[str, Any], event: dict[str, Any]) -> None:
    rows = entry.get("history")
    history = list(rows) if isinstance(rows, list) else []
    history.append(event)
    entry["history"] = history[-MAX_HISTORY:]


def load_grant(repo_key: str, root: Path | None = None) -> AutoGrant | None:
    return _grant_from(repo_key, _read(consent_path(root))["repositories"].get(repo_key))


def list_grants(root: Path | None = None) -> list[AutoGrant]:
    repos = _read(consent_path(root))["repositories"]
    grants = [_grant_from(str(key), entry) for key, entry in repos.items()]
    return [grant for grant in grants if grant is not None]


def consent_history(repo_key: str, root: Path | None = None) -> list[dict[str, Any]]:
    entry = _read(consent_path(root))["repositories"].get(repo_key)
    rows = entry.get("history") if isinstance(entry, dict) else None
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def grant_auto(
    repo: RepositoryIdentity,
    pairs: Iterable[tuple[str, str]],
    *,
    via: str = "cli",
    root: Path | None = None,
    actor: str | None = None,
) -> AutoGrant:
    """Record an explicit Auto opt-in for ``pairs`` in one repository.

    Grants accumulate: re-running ``lc rt savings auto`` adds newly Ready pairs
    to the existing opt-in. Nothing outside this call ever adds a pair.
    """
    requested = _pairs([list(pair) for pair in pairs])
    if not requested:
        raise ValueError("an Auto opt-in needs at least one (model, kind) pair")
    path = consent_path(root)
    who = actor or local_actor()
    with _cross_process_lock(path):
        raw = _read(path)
        repos = raw["repositories"]
        entry = _entry(repos, repo.key, repo_root=repo.root, common_dir=repo.common_dir)
        existing = _grant_from(repo.key, entry)
        merged = tuple(dict.fromkeys((*(existing.pairs if existing else ()), *requested)))
        at = _now()
        entry["grant"] = {
            "granted_at": at,
            "granted_by": who,
            "via": via,
            "pairs": [list(pair) for pair in merged],
        }
        _history(entry, {"action": "grant", "at": at, "by": who, "via": via, "pairs": [list(p) for p in requested]})
        _bound(repos)
        _atomic_write(path, raw)
    grant = _grant_from(repo.key, entry)
    assert grant is not None
    return grant


def revoke_auto(repo_key: str, *, via: str = "cli", root: Path | None = None, actor: str | None = None) -> bool:
    path = consent_path(root)
    who = actor or local_actor()
    with _cross_process_lock(path):
        raw = _read(path)
        entry = raw["repositories"].get(repo_key)
        if not isinstance(entry, dict) or not isinstance(entry.get("grant"), dict):
            return False
        entry.pop("grant", None)
        entry["updated_at"] = _now()
        _history(entry, {"action": "revoke", "at": entry["updated_at"], "by": who, "via": via})
        _atomic_write(path, raw)
    return True


def revoke_all_auto(*, via: str = "cli", root: Path | None = None, actor: str | None = None) -> int:
    path = consent_path(root)
    who = actor or local_actor()
    revoked = 0
    with _cross_process_lock(path):
        raw = _read(path)
        for entry in raw["repositories"].values():
            if not isinstance(entry, dict) or not isinstance(entry.get("grant"), dict):
                continue
            entry.pop("grant", None)
            entry["updated_at"] = _now()
            _history(entry, {"action": "revoke", "at": entry["updated_at"], "by": who, "via": via})
            revoked += 1
        if revoked:
            _atomic_write(path, raw)
    return revoked


def claim_consent_notice(repo: RepositoryIdentity, root: Path | None = None) -> bool:
    """True exactly once per repository: the migration/opt-in question is asked once."""
    path = consent_path(root)
    with _cross_process_lock(path):
        raw = _read(path)
        repos = raw["repositories"]
        existing = repos.get(repo.key)
        if isinstance(existing, dict) and existing.get("notified_at"):
            return False
        entry = _entry(repos, repo.key, repo_root=repo.root, common_dir=repo.common_dir)
        entry["notified_at"] = entry["updated_at"]
        _bound(repos)
        _atomic_write(path, raw)
    return True


def consent_notice_for(gate: AutoGate | None, root: Path | None = None) -> str:
    """The one-time opt-in question for a repository where Auto was requested."""
    if gate is None or gate.reason != "repository_not_opted_in" or gate.repo is None:
        return ""
    return CONSENT_NOTICE if claim_consent_notice(gate.repo, root=root) else ""


def repository_window(
    model: str,
    kind: RepairKind,
    repo_key: str,
    settings: VerifiedWorkSettings,
    *,
    root: Path | None = None,
) -> ModelOutcomeStats:
    """This repository's recent window, sized like the global qualification window."""
    return load_repository_outcome(model, kind, repo_key, root=root, window=max(20, settings.auto_min_samples))


def window_qualifies(stats: ModelOutcomeStats, settings: VerifiedWorkSettings) -> bool:
    return (
        stats.attempts >= settings.auto_min_samples
        and stats.verification_rate >= settings.auto_min_success_rate
        and stats.average_latency_ms <= settings.auto_max_avg_latency_ms
    )


def auto_gate(
    settings: VerifiedWorkSettings,
    *,
    workspace: Path | str,
    kind: RepairKind,
    host: str,
    model: str | None = None,
    repo: RepositoryIdentity | None = None,
    root: Path | None = None,
) -> AutoGate:
    """Decide whether Auto may promote a ``kind`` repair in ``workspace``.

    With ``model`` the decision is for that exact worker; without it the gate
    returns every opted-in model whose repository window qualifies.
    """
    if settings.mode != "enforce":
        return AutoGate(False, "mode_not_enforce")
    if host not in AUTO_HOSTS:
        return AutoGate(False, "host_unsupported")
    identity = repo or repository_identity(workspace)
    if identity is None:
        return AutoGate(False, "no_repository")
    grant = load_grant(identity.key, root=root)
    if grant is None:
        return AutoGate(False, "repository_not_opted_in", identity)
    granted = [pair_model for pair_model, pair_kind in grant.pairs if pair_kind == kind]
    if not granted:
        return AutoGate(False, "kind_not_opted_in", identity)
    if model is not None:
        if model not in granted:
            return AutoGate(False, "model_not_opted_in", identity)
        granted = [model]
    qualified = tuple(
        name
        for name in granted
        if window_qualifies(repository_window(name, kind, identity.key, settings, root=root), settings)
    )
    if not qualified:
        return AutoGate(False, "repository_window_not_qualified", identity)
    return AutoGate(True, "auto_opted_in", identity, qualified)


__all__ = [
    "AUTO_HOSTS",
    "CONSENT_NOTICE",
    "HOST_SUPPORT",
    "MAX_HISTORY",
    "MAX_REPOSITORIES",
    "AutoGate",
    "AutoGrant",
    "RepositoryIdentity",
    "auto_gate",
    "claim_consent_notice",
    "consent_history",
    "consent_notice_for",
    "consent_path",
    "grant_auto",
    "list_grants",
    "load_grant",
    "local_actor",
    "repository_identity",
    "repository_window",
    "revoke_all_auto",
    "revoke_auto",
    "window_qualifies",
]
