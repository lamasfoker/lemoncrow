"""Human approval before risky authoring-agent actions (Phase 12 item 14).

While an agent works on a Change it can be paused, by the agent host's own
supervision hook, before an action in a *configured* risk class: dependency
install/upgrade, schema/migration, network/cloud CLI, secret access, git
push/force-push, or a write outside the workspace. A human decides in the
Review; the decision is an ``agent_approval`` record (``review_derived``, moved
only by compare-and-set) plus an ``approval`` Development event, and the later
*observed* side effect is shown as pre-approved only when it is exactly what was
approved.

Conservative by construction (each rule is a recorded P12-AGENT interpretation):

* Classes are opt-in per installation (``review.approval.<class>`` settings,
  default OFF). An unconfigured class never pauses anything.
* Normalization is minimal: line endings and outer whitespace only. No shell
  re-quoting, path ``..`` folding or symlink resolution, so two texts that could
  mean different things are never treated as the same action.
* An approval is bounded in time (``valid_until``) and in scope: one exact
  action used once, or an explicit pattern whose ``*`` matches exactly one plain
  token (never a flag, never a shell metacharacter) or one path component.
* A pre-approval covers an observed side effect only for the same agent session,
  inside its validity window, by exact action digest (``once``) or by the pattern
  over the *untruncated* observed text. Anything else is shown unapproved.

This module is pure policy and record logic; routes live in
``action_approval_api.py`` and the host hook in ``approval_hook.py``.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.derived_records import DerivedRecordLedger

RiskClass = Literal[
    "dependency_install", "schema_migration", "network_cloud", "secret_access", "git_push", "outside_write"
]
RISK_CLASSES: Final[tuple[str, ...]] = (
    "dependency_install",
    "schema_migration",
    "network_cloud",
    "secret_access",
    "git_push",
    "outside_write",
)
RISK_LABELS: Final[dict[str, str]] = {
    "dependency_install": "Dependency install or upgrade",
    "schema_migration": "Schema or migration command",
    "network_cloud": "Network or cloud CLI",
    "secret_access": "Secret access",
    "git_push": "Git push",
    "outside_write": "Write outside the workspace",
    "supervision_change": "Change to approval supervision",
}
SUPERVISION_CHANGE: Final[str] = "supervision_change"
"""Hook-only class: turning approval off (or the hook itself) needs approval while any class is on."""
GATED_CLASSES: Final[tuple[str, ...]] = (*RISK_CLASSES, SUPERVISION_CHANGE)

# Agent-ledger categories (``agent_ledger.LEDGER_CATEGORIES``) to approval classes.
LEDGER_TO_RISK: Final[dict[str, str]] = {
    "package_install": "dependency_install",
    "database": "schema_migration",
    "network": "network_cloud",
    "cloud": "network_cloud",
    "secret_access": "secret_access",
    "git_push": "git_push",
    "outside_write": "outside_write",
}

SETTING_PREFIX: Final[str] = "review.approval."
WAIT_SETTING: Final[str] = "review.approval.wait_seconds"
DEFAULT_WAIT_SECONDS: Final[int] = 120
MIN_WAIT_SECONDS: Final[int] = 5
MAX_WAIT_SECONDS: Final[int] = 900
"""The hook's own wait for a decision; the host hook timeout is set above this."""
DEFAULT_DECISION_TTL: Final[int] = 900
MIN_DECISION_TTL: Final[int] = 60
MAX_DECISION_TTL: Final[int] = 86_400
PENDING_TTL_SECONDS: Final[int] = 1_800
"""A request nobody decided expires; the hook has long stopped waiting by then."""
MAX_ACTION_CHARS: Final[int] = 4_000
MAX_PATTERN_TOKENS: Final[int] = 32
MAX_PATTERN_WILDCARDS: Final[int] = 4
MAX_PENDING_PER_SESSION: Final[int] = 16
MAX_APPROVALS_PER_REVIEW: Final[int] = 1_000
# lc-debt: approval records of one Review are read whole for coverage (bounded by
# MAX_APPROVALS_PER_REVIEW, refused visibly past it); upgrade path: an indexed
# per-session table if Reviews ever need more.
MAX_NOTE_CHARS: Final[int] = 500

APPROVAL_KIND: Final[str] = "agent_approval"
ActionKind = Literal["command", "write"]
ACTION_KINDS: Final[tuple[str, ...]] = ("command", "write")
SCOPES: Final[tuple[str, ...]] = ("once", "pattern")


class ApprovalConfigError(RuntimeError):
    """The approval settings exist but cannot be read; configured classes fail closed."""


# --------------------------------------------------------------------------- #
# Policy (settings layer)                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Which classes pause for a human, and how long the hook waits for one."""

    classes: frozenset[str] = frozenset()
    wait_seconds: int = DEFAULT_WAIT_SECONDS

    def required(self, found: Iterable[str]) -> frozenset[str]:
        """The configured classes among *found*; supervision changes count once any class is on."""

        hits = frozenset(found)
        out = hits & self.classes
        if self.classes and SUPERVISION_CHANGE in hits:
            out |= {SUPERVISION_CHANGE}
        return out

    def to_wire(self) -> dict[str, Any]:
        return {"classes": sorted(self.classes), "wait_seconds": self.wait_seconds}


def clamp_wait(value: object) -> int:
    """The hook wait in seconds, clamped to ``[MIN_WAIT_SECONDS, MAX_WAIT_SECONDS]``."""

    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, seconds))


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def policy_from_settings(raw: Mapping[str, Any]) -> ApprovalPolicy:
    """The approval policy carried by one settings mapping (``plugin_settings.json`` keys)."""

    classes = frozenset(name for name in RISK_CLASSES if _truthy(raw.get(SETTING_PREFIX + name, False)))
    return ApprovalPolicy(classes=classes, wait_seconds=clamp_wait(raw.get(WAIT_SETTING, DEFAULT_WAIT_SECONDS)))


def read_policy(root: str | Path) -> ApprovalPolicy:
    """Read the approval policy strictly: a missing file is OFF, an unreadable one raises.

    ``lemoncrow.core.settings.load_raw`` treats a corrupt file as empty, which for
    approval would silently turn every configured class off (fail open). Here the
    caller gets :class:`ApprovalConfigError` and fails closed instead.
    """

    from lemoncrow.core.settings import settings_path

    path = settings_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ApprovalPolicy()
    except OSError as exc:
        raise ApprovalConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApprovalConfigError(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ApprovalConfigError(f"{path} does not hold a settings object")
    nested = data.get("lemoncrow")
    merged = {**data, **nested} if isinstance(nested, dict) else data
    return policy_from_settings(merged)


# --------------------------------------------------------------------------- #
# Actions: normalization, digest, classification                              #
# --------------------------------------------------------------------------- #


def normalize_action(kind: str, text: str) -> str:
    """Line endings and outer whitespace only (see module docstring)."""

    if kind not in ACTION_KINDS:
        raise ValueError(f"action kind must be one of {', '.join(ACTION_KINDS)}")
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def action_digest(kind: str, text: str) -> str:
    """The exact identity of one normalized action (hex SHA-256 over kind and text)."""

    normalized = normalize_action(kind, text)
    return hashlib.sha256(f"{kind}\0{normalized}".encode()).hexdigest()


_SUPERVISION_FILES = re.compile(
    r"(?:^|[/\s'\"=])(?:plugin_settings\.json|settings(?:\.local)?\.json|hooks\.json|approval_gate\.(?:py|sh)|_run_hook\.sh)"
    r"(?:$|[\s'\";|&)])"
)
_SETTINGS_COMMAND = re.compile(r"\b(?:lc|lemoncrow)\s+settings\s+(?:set|unset|reset|edit|import)\b")


def _supervision_command(command: str) -> bool:
    # lc-debt: detects the obvious ways to switch approval off (the settings CLI,
    # writes naming the settings/hook files); an agent that rewrites them through
    # an interpreter is not seen. This raises the bar; it is not a security
    # boundary. Upgrade: host-enforced managed settings.
    return bool(_SETTINGS_COMMAND.search(command) and "review.approval" in command) or bool(
        _SUPERVISION_FILES.search(command)
    )


def _supervision_path(path: str) -> bool:
    name = posixpath.basename(path.strip())
    if name == "plugin_settings.json":
        return True
    parent = posixpath.basename(posixpath.dirname(path.strip()))
    return (parent == ".claude" and name in {"settings.json", "settings.local.json"}) or (
        parent == "hooks" and name in {"hooks.json", "approval_gate.py", "approval_gate.sh", "_run_hook.sh"}
    )


def unclassifiable_parts(kind: str, text: str) -> tuple[str, ...]:
    """Why a command's effect cannot be read from its text (empty: it can).

    A command word computed at run time (``$(echo pip) install x``, ``$CMD``),
    commands piped or redirected into a shell, a command that runs a file or
    another interpreter (``bash x.sh``, ``source f``, ``./run``, ``make``,
    ``npm run x``, ``python file.py``, ``node -e ...``; never read, because the
    only copy at gate time is the agent's mutable worktree), or command text
    nested deeper than ``agent_ledger.MAX_SHELL_DEPTH``. :func:`classify_action` counts such a
    command as every risk class, so each configured class gates it (fail closed).
    """

    from lemoncrow.pro.capabilities.review.agent_ledger import analyze_command

    normalized = normalize_action(kind, text)
    if kind != "command" or not normalized:
        return ()
    return analyze_command(normalized).unresolved


def classify_action(kind: str, text: str, *, repo_root: str = "") -> frozenset[str]:
    """Every gated class one action falls in (empty: an ordinary action).

    Wrapped commands (``bash -lc``, ``eval``, ``xargs``, ``sudo -u`` ...) are
    classified by what they run; a command whose effect cannot be read from its
    text (:func:`unclassifiable_parts`) is every risk class.
    """

    from lemoncrow.pro.capabilities.review.agent_ledger import analyze_command, classify_outside_write

    normalized = normalize_action(kind, text)
    if not normalized:
        return frozenset()
    found: set[str] = set()
    if kind == "command":
        analysis = analyze_command(normalized, repo_root=repo_root)
        for item in analysis.classes:
            found.add(LEDGER_TO_RISK[item.category])
        if analysis.unresolved:
            found.update(RISK_CLASSES)
        if _supervision_command(normalized):
            found.add(SUPERVISION_CHANGE)
    else:
        hit = classify_outside_write(normalized, repo_root)
        if hit is not None:
            found.add(LEDGER_TO_RISK[hit.category])
        if _supervision_path(normalized):
            found.add(SUPERVISION_CHANGE)
    return frozenset(found)


# --------------------------------------------------------------------------- #
# Scoped patterns                                                              #
# --------------------------------------------------------------------------- #

_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_./:@=+,%-]+")
_PATH_COMPONENT = re.compile(r"[^/\0*]+")


def _plain_tokens(text: str) -> list[str] | None:
    """Single-space separated plain words, or ``None`` (quoting, operators, globs...)."""

    tokens = text.split(" ")
    if not text or " ".join(tokens) != text or any(not _PLAIN_TOKEN.fullmatch(token) for token in tokens):
        return None
    return tokens


def _clean_absolute(path: str) -> bool:
    if not path.startswith("/") or "//" in path or (path.endswith("/") and path != "/"):
        return False
    return all(part not in {".", ".."} for part in path.split("/")[1:])


def validate_pattern(kind: str, pattern: str, action: str) -> str:
    """The pattern as stored, or :class:`ValueError` with the reason it is refused."""

    text = normalize_action(kind, pattern)
    if kind == "command":
        tokens = text.split(" ")
        if len(tokens) > MAX_PATTERN_TOKENS:
            raise ValueError(f"a command pattern has at most {MAX_PATTERN_TOKENS} words")
        literal = [token for token in tokens if token != "*"]
        wildcards = len(tokens) - len(literal)
        if (
            " ".join(tokens) != text
            or not tokens
            or tokens[0] == "*"
            or any(not _PLAIN_TOKEN.fullmatch(token) for token in literal)
        ):
            raise ValueError(
                "a command pattern is plain words separated by single spaces, starting with the program; "
                "* stands for exactly one word"
            )
        if wildcards > MAX_PATTERN_WILDCARDS:
            raise ValueError(f"a command pattern has at most {MAX_PATTERN_WILDCARDS} * wildcards")
    else:
        body = text[:-2] if text.endswith("/*") else text
        if "*" in body or not _clean_absolute(body) or body == "/":
            raise ValueError(
                "a write pattern is an absolute path, or a directory followed by /* for exactly one file in it"
            )
    if not pattern_covers(kind, text, action):
        raise ValueError("the pattern must cover the action it approves")
    return text


def pattern_covers(kind: str, pattern: str, action: str) -> bool:
    """Whether one explicit scoped pattern covers one normalized action."""

    normalized = normalize_action(kind, action)
    if kind == "command":
        wanted = pattern.split(" ")
        tokens = _plain_tokens(normalized)
        if tokens is None or len(tokens) != len(wanted) or not wanted or wanted[0] == "*":
            return False
        for expected, actual in zip(wanted, tokens, strict=True):
            if expected == "*":
                if actual.startswith("-"):
                    return False  # a wildcard never stands for a flag such as --force
            elif expected != actual:
                return False
        return True
    if not _clean_absolute(normalized):
        return False
    if pattern.endswith("/*"):
        directory = pattern[:-2]
        return posixpath.dirname(normalized) == directory and bool(
            _PATH_COMPONENT.fullmatch(posixpath.basename(normalized))
        )
    return normalized == pattern


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #


def approval_ledger(store: Any) -> DerivedRecordLedger:
    return DerivedRecordLedger(store, APPROVAL_KIND)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def parse_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def effective_status(record: Mapping[str, Any], now: datetime) -> str:
    """``pending`` | ``approved`` | ``denied`` | ``expired`` at *now* (expiry is lazy)."""

    status = str(record.get("status") or "")
    if status == "pending":
        expires = parse_at(record.get("expires_at"))
        return "expired" if expires is None or now >= expires else "pending"
    if status == "approved":
        until = parse_at(record.get("valid_until"))
        return "expired" if until is None or now >= until else "approved"
    return status if status in {"denied", "expired"} else "expired"


def new_request(
    *,
    approval_id: str,
    review_id: str,
    host: str,
    session_id: str,
    kind: str,
    action: str,
    cwd: str,
    classes: Iterable[str],
    requested_by: str,
    now: datetime,
) -> dict[str, Any]:
    normalized = normalize_action(kind, action)
    ordered = [name for name in GATED_CLASSES if name in set(classes)]
    return {
        "id": approval_id,
        "review_id": review_id,
        "status": "pending",
        "risk_class": ordered[0] if ordered else "",
        "classes": ordered,
        "kind": kind,
        "action": normalized,
        "action_digest": action_digest(kind, normalized),
        "cwd": cwd,
        "host": host,
        "session_id": session_id,
        "requested_by": requested_by,
        "requested_at": iso(now),
        "expires_at": iso(now + timedelta(seconds=PENDING_TTL_SECONDS)),
        "decided_by": "",
        "decided_kind": "",
        "decided_at": "",
        "scope": "",
        "pattern": "",
        "valid_until": "",
        "claimed_at": "",
        "note": "",
    }


def action_allowed_by(record: Mapping[str, Any], *, kind: str, action: str, cwd: str) -> bool:
    """Whether an approved record's scope covers this exact request (same cwd, same text or pattern)."""

    if str(record.get("kind") or "") != kind or str(record.get("cwd") or "") != cwd:
        return False
    if record.get("scope") == "once":
        return str(record.get("action_digest") or "") == action_digest(kind, action)
    if record.get("scope") == "pattern":
        return pattern_covers(kind, str(record.get("pattern") or ""), action)
    return False


def reusable_approval(
    records: Iterable[Mapping[str, Any]],
    *,
    host: str,
    session_id: str,
    kind: str,
    action: str,
    cwd: str,
    classes: Iterable[str],
    now: datetime,
) -> Mapping[str, Any] | None:
    """A still-valid human approval that already covers this request, if any."""

    wanted = frozenset(classes)
    for record in records:
        if (
            effective_status(record, now) == "approved"
            and record.get("host") == host
            and record.get("session_id") == session_id
            and wanted <= frozenset(record.get("classes") or ())
            and action_allowed_by(record, kind=kind, action=action, cwd=cwd)
            and not (record.get("scope") == "once" and record.get("claimed_at"))
        ):
            return record
    return None


def approval_wire(record: Mapping[str, Any], now: datetime, *, revision_id: str = "") -> dict[str, Any]:
    """The Reader/hook view of one record; ``status`` is the effective status."""

    status = effective_status(record, now)
    classes = [str(item) for item in record.get("classes") or ()]
    return {
        "id": str(record.get("id") or ""),
        "status": status,
        "used": bool(record.get("claimed_at")) and record.get("scope") == "once",
        "risk_class": str(record.get("risk_class") or ""),
        "risk_label": RISK_LABELS.get(str(record.get("risk_class") or ""), ""),
        "classes": classes,
        "kind": str(record.get("kind") or ""),
        "action": str(record.get("action") or ""),
        "action_digest": str(record.get("action_digest") or ""),
        "cwd": str(record.get("cwd") or ""),
        "host": str(record.get("host") or ""),
        "session_id": str(record.get("session_id") or ""),
        "requested_by": str(record.get("requested_by") or ""),
        "requested_at": str(record.get("requested_at") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        "decided_by": str(record.get("decided_by") or ""),
        "decided_at": str(record.get("decided_at") or ""),
        "scope": str(record.get("scope") or ""),
        "pattern": str(record.get("pattern") or ""),
        "valid_until": str(record.get("valid_until") or ""),
        "claimed_at": str(record.get("claimed_at") or ""),
        "note": str(record.get("note") or ""),
        "revision_id": revision_id,
    }


# --------------------------------------------------------------------------- #
# Coverage of observed side effects                                            #
# --------------------------------------------------------------------------- #

OBSERVED_SOURCE: Final[str] = "run_ledger"
"""Only side effects LemonCrow observed itself can be covered; self-reported ones cannot."""


def required_classes(
    records: Iterable[Mapping[str, Any]],
    policy: ApprovalPolicy | None,
    *,
    host: str,
    session_id: str,
) -> frozenset[str]:
    """Classes that needed approval for one session: configured here, or asked for by its hook."""

    out: set[str] = set(policy.classes if policy is not None else RISK_CLASSES)
    for record in records:
        if record.get("host") == host and record.get("session_id") == session_id:
            out.update(str(item) for item in record.get("classes") or ())
    return frozenset(out)


def _covering_record(
    row: Mapping[str, Any],
    risk: str,
    records: Sequence[Mapping[str, Any]],
    used: set[str],
) -> Mapping[str, Any] | None:
    at = parse_at(row.get("at"))
    digest = str(row.get("action_digest") or "")
    detail = str(row.get("detail") or "")
    if at is None or not digest or str(row.get("source") or "") != OBSERVED_SOURCE:
        return None
    for record in records:
        if (
            record.get("status") != "approved"
            or record.get("host") != row.get("host")
            or record.get("session_id") != row.get("session_id")
            or risk not in (record.get("classes") or ())
        ):
            continue
        decided = parse_at(record.get("decided_at"))
        until = parse_at(record.get("valid_until"))
        if decided is None or until is None or not decided <= at < until:
            continue
        if record.get("scope") == "once":
            claimed = parse_at(record.get("claimed_at"))
            if (
                str(record.get("id")) not in used
                and claimed is not None
                and claimed <= at
                and record.get("action_digest") == digest
            ):
                used.add(str(record.get("id")))
                return record
        elif record.get("scope") == "pattern":
            kind = str(record.get("kind") or "")
            # The pattern is matched against the observed text only when that text
            # is complete: its digest must recompute to the one taken at observation.
            if (
                kind in ACTION_KINDS
                and action_digest(kind, detail) == digest
                and pattern_covers(kind, str(record.get("pattern") or ""), detail)
            ):
                return record
    return None


def annotate_observed(
    rows: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    policy: ApprovalPolicy | None,
) -> list[dict[str, Any]]:
    """Each observed side-effect row with ``approval``: pre-approved, unapproved, or ``None``.

    ``None`` means approval was never required for that row's class and session.
    A ``None`` *policy* (unreadable settings) treats every class as required.
    Rows are matched oldest first, so a ``once`` approval covers only the first
    exactly matching action after it was used.
    """

    used: set[str] = set()
    approved = [record for record in records if record.get("status") == "approved"]
    order = sorted(range(len(rows)), key=lambda index: (str(rows[index].get("at") or ""), index))
    out: list[dict[str, Any]] = [dict(row) for row in rows]
    for index in order:
        row = rows[index]
        risk = LEDGER_TO_RISK.get(str(row.get("category") or row.get("status") or ""), "")
        host, session_id = str(row.get("host") or ""), str(row.get("session_id") or "")
        if not risk:
            out[index]["approval"] = None
            continue
        record = _covering_record(row, risk, approved, used)
        if record is not None:
            out[index]["approval"] = {
                "status": "pre_approved",
                "approval_id": str(record.get("id") or ""),
                "decided_by": str(record.get("decided_by") or ""),
                "decided_at": str(record.get("decided_at") or ""),
                "scope": str(record.get("scope") or ""),
                "pattern": str(record.get("pattern") or ""),
            }
        elif risk in required_classes(records, policy, host=host, session_id=session_id):
            out[index]["approval"] = {"status": "unapproved"}
        else:
            out[index]["approval"] = None
    return out


__all__ = [
    "ACTION_KINDS",
    "APPROVAL_KIND",
    "DEFAULT_DECISION_TTL",
    "DEFAULT_WAIT_SECONDS",
    "GATED_CLASSES",
    "LEDGER_TO_RISK",
    "MAX_ACTION_CHARS",
    "MAX_APPROVALS_PER_REVIEW",
    "MAX_DECISION_TTL",
    "MAX_PATTERN_TOKENS",
    "MAX_PATTERN_WILDCARDS",
    "MAX_PENDING_PER_SESSION",
    "MAX_WAIT_SECONDS",
    "MIN_DECISION_TTL",
    "MIN_WAIT_SECONDS",
    "PENDING_TTL_SECONDS",
    "RISK_CLASSES",
    "RISK_LABELS",
    "SCOPES",
    "SETTING_PREFIX",
    "SUPERVISION_CHANGE",
    "WAIT_SETTING",
    "ApprovalConfigError",
    "ApprovalPolicy",
    "action_allowed_by",
    "action_digest",
    "annotate_observed",
    "approval_ledger",
    "approval_wire",
    "clamp_wait",
    "classify_action",
    "effective_status",
    "new_request",
    "normalize_action",
    "parse_at",
    "pattern_covers",
    "policy_from_settings",
    "read_policy",
    "required_classes",
    "reusable_approval",
    "validate_pattern",
]
