"""Project observable author-session context into an exact Review packet.

The Review journal is intentionally *not* a transcript dump.  It carries the
bounded, human-review-relevant facts that LemonCrow already observed while the
change was authored: user intent, explicit plan/decisions/facts/questions,
changed-file edits, and verification/command outcomes.  Provider-private
reasoning / hidden chain-of-thought is never imported.

Attachment is fail-closed.  We only read the exact author session selected by
Review provenance, and a ledger without a recorded workspace must itself show
an edit to one of the reviewed paths before any session-wide context is allowed
through.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from lemoncrow_client.kit.redaction import redact

from lemoncrow.core.foundation.paths import find_session_dir, session_dir

from .models import ProvenanceRecord, ReviewJournalEntry, ReviewJournalKind

_MAX_ENTRIES = 48
_MAX_SUMMARY = 500
_MAX_DETAIL = 1600
_MAX_PLAN_STEPS = 12
_MAX_STATE_ROWS = 12
_MAX_EVENT_ROWS = 32


def _bounded(value: object, limit: int) -> str:
    # Review is durable and human-visible. Remove credentials and hidden
    # reasoning markers before any observable text is frozen or returned live.
    text = redact(str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _relative_path(repo_root: Path, raw: str) -> str:
    path = _normalize_path(raw)
    if not path:
        return ""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            return path
    return PurePosixPath(path).as_posix().lstrip("./")


def _matches_reviewed_path(repo_root: Path, raw: str, reviewed: frozenset[str]) -> str:
    candidate = _relative_path(repo_root, raw)
    if not candidate:
        return ""
    if candidate in reviewed:
        return candidate
    # Hook paths may be absolute or workspace-prefixed while packet paths are
    # repository-relative. A suffix match is valid only on a complete path
    # component boundary.
    hits = [path for path in reviewed if candidate.endswith("/" + path)]
    return hits[0] if len(hits) == 1 else ""


def _entry_id(session_id: str, kind: str, at: str, summary: str, path: str) -> str:
    raw = "\0".join((session_id, kind, at, summary, path))
    return "jrnl-" + hashlib.sha256(raw.encode("utf-8", errors="surrogateescape")).hexdigest()[:24]


def _entry(
    *,
    host: str,
    session_id: str,
    kind: ReviewJournalKind,
    at: str,
    summary: object,
    detail: object = "",
    path: str = "",
    status: str = "",
    source: str = "run_ledger",
) -> ReviewJournalEntry | None:
    summary_text = _bounded(summary, _MAX_SUMMARY)
    if not summary_text:
        return None
    detail_text = _bounded(detail, _MAX_DETAIL)
    return ReviewJournalEntry(
        id=_entry_id(session_id, kind, at, summary_text, path),
        kind=kind,
        at=at,
        summary=summary_text,
        detail=detail_text,
        path=path,
        status=_bounded(status, 32).lower(),
        source=source,
        host=host,
        session_id=session_id,
    )


def _run_path(store_root: Path, host: str, session_id: str) -> Path | None:
    exact = session_dir(store_root, host, session_id) / "run.json"
    if exact.is_file():
        return exact
    found = find_session_dir(store_root, session_id)
    if found is None or found.parent.name != host:
        return None
    candidate = found / "run.json"
    return candidate if candidate.is_file() else None


def _read_run(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _event_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _status_for_command(payload: Mapping[str, Any]) -> str:
    ok = payload.get("ok")
    if isinstance(ok, bool):
        return "pass" if ok else "fail"
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "pass" if exit_code == 0 else "fail"
    return ""


def _event_entry(
    raw: Mapping[str, Any],
    *,
    host: str,
    session_id: str,
    repo_root: Path,
    reviewed: frozenset[str],
) -> ReviewJournalEntry | None:
    kind = str(raw.get("kind") or "").strip()
    # Explicitly exclude private/model reasoning even when a host happens to
    # expose it in a trace. Review carries decisions and observable outcomes,
    # not hidden chain-of-thought.
    if kind in {"reasoning", "checkpoint", "rubric_run", "watchdog_alert"}:
        return None
    at = _bounded(raw.get("at"), 80)
    summary = _bounded(raw.get("summary"), _MAX_SUMMARY)
    payload = _event_payload(raw)

    if kind in {"file_edit", "file_revert"}:
        path = _matches_reviewed_path(repo_root, str(payload.get("path") or ""), reviewed)
        if not path:
            return None
        action = "edited" if kind == "file_edit" else "reverted"
        return _entry(
            host=host,
            session_id=session_id,
            kind="edit",
            at=at,
            summary=f"{action} {path}",
            path=path,
            status="changed" if kind == "file_edit" else "reverted",
        )

    if kind in {"command", "command_result"}:
        status = _status_for_command(payload)
        detail = payload.get("error_signature") or ("command failed" if status == "fail" else "")
        return _entry(
            host=host,
            session_id=session_id,
            kind="command",
            at=at,
            summary=summary,
            detail=detail,
            status=status,
        )

    if kind in {"test_result", "validation"}:
        passed = payload.get("passed")
        status = "pass" if passed is True else "fail" if passed is False else _status_for_command(payload)
        detail = payload.get("detail") or payload.get("error") or payload.get("message") or ""
        return _entry(
            host=host,
            session_id=session_id,
            kind="validation",
            at=at,
            summary=summary,
            detail=detail,
            status=status,
        )

    if kind == "agent_message":
        role = str(payload.get("role") or "").strip().lower()
        if role == "user":
            text = payload.get("prompt") or payload.get("message") or summary.removeprefix("user:").strip()
            return _entry(
                host=host,
                session_id=session_id,
                kind="intent",
                at=at,
                summary=text,
                source="user",
            )
        if role in {"assistant", "agent"}:
            text = payload.get("message") or payload.get("text") or payload.get("response") or summary
            return _entry(
                host=host,
                session_id=session_id,
                kind="message",
                at=at,
                summary=text,
                source="agent",
            )
        return None

    if kind == "route_decision":
        return _entry(host=host, session_id=session_id, kind="decision", at=at, summary=summary, detail=payload)

    if kind == "model_recommendation":
        return _entry(host=host, session_id=session_id, kind="decision", at=at, summary=summary)

    if kind == "note":
        # Workflow notes are explicit state transitions; arbitrary notes are too
        # unconstrained to put in front of a reviewer by default.
        if not summary.startswith(("workflow:", "plan_review:", "task_progress:", "workflow_step:")):
            return None
        return _entry(host=host, session_id=session_id, kind="decision", at=at, summary=summary, detail=payload)

    if kind in {"tool_call", "tool_result"}:
        # Tool chatter is enormous. Retain only calls that explicitly name a
        # reviewed path; the detail stays a summary rather than raw output so
        # logs/secrets do not get duplicated into Review artifacts.
        serial = json.dumps(payload, ensure_ascii=False, default=str)
        path = next((item for item in reviewed if item in serial), "")
        if not path:
            return None
        tool = str(payload.get("tool") or payload.get("name") or summary).strip()
        return _entry(
            host=host,
            session_id=session_id,
            kind="tool",
            at=at,
            summary=tool,
            path=path,
            source="run_ledger",
        )
    return None


def _state_entries(
    run: Mapping[str, Any],
    *,
    host: str,
    session_id: str,
) -> list[ReviewJournalEntry]:
    at = _bounded(run.get("updated_at") or run.get("created_at"), 80)
    rows: list[ReviewJournalEntry] = []

    plan = [str(item).strip() for item in (run.get("current_plan") or ()) if str(item).strip()][:_MAX_PLAN_STEPS]
    if plan:
        item = _entry(
            host=host,
            session_id=session_id,
            kind="plan",
            at=at,
            summary="Author plan",
            detail="\n".join(f"{index}. {step}" for index, step in enumerate(plan, start=1)),
        )
        if item is not None:
            rows.append(item)

    fields: tuple[tuple[str, ReviewJournalKind, str], ...] = (
        ("verified_facts", "fact", "Verified"),
        ("hypotheses_rejected", "decision", "Rejected"),
        ("open_questions", "question", "Open question"),
        ("current_blockers", "blocker", "Blocker"),
    )
    for field, kind, prefix in fields:
        values = run.get(field) or ()
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for value in list(values)[:_MAX_STATE_ROWS]:
            text = str(value).strip()
            if not text:
                continue
            item = _entry(host=host, session_id=session_id, kind=kind, at=at, summary=f"{prefix}: {text}")
            if item is not None:
                rows.append(item)

    next_validation = str(run.get("next_required_validation") or "").strip()
    if next_validation:
        item = _entry(
            host=host,
            session_id=session_id,
            kind="validation",
            at=at,
            summary=f"Next validation: {next_validation}",
            status="pending",
        )
        if item is not None:
            rows.append(item)
    return rows


def _dedupe(items: Iterable[ReviewJournalEntry]) -> tuple[ReviewJournalEntry, ...]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[ReviewJournalEntry] = []
    for item in items:
        key = (item.kind, item.summary, item.path, item.status)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    # Synthetic state rows carry ``updated_at`` and therefore naturally sit
    # beside the latest observable events. Stable id breaks timestamp ties.
    out.sort(key=lambda item: (item.at, item.id))
    return tuple(out[-_MAX_ENTRIES:])


def collect_review_journal(
    store_root: Path,
    repo_root: Path,
    provenance: ProvenanceRecord,
    packet_paths: Sequence[str],
) -> tuple[ReviewJournalEntry, ...]:
    """Return the bounded observable journal for one exact author session."""

    if provenance.certainty != "exact" or not provenance.session_id or not provenance.host:
        return ()
    host = provenance.host.strip().lower()
    session_id = provenance.session_id.strip()
    if not host or not session_id:
        return ()
    run_path = _run_path(store_root, host, session_id)
    if run_path is None:
        return ()
    run = _read_run(run_path)
    if run is None:
        return ()

    root = repo_root.expanduser().resolve()
    reviewed = frozenset(_normalize_path(path) for path in packet_paths if _normalize_path(path))
    raw_events = run.get("events") or ()
    events = [item for item in raw_events if isinstance(item, Mapping)] if isinstance(raw_events, Sequence) else []
    relevant_edit = any(
        str(event.get("kind") or "") in {"file_edit", "file_revert"}
        and bool(_matches_reviewed_path(root, str(_event_payload(event).get("path") or ""), reviewed))
        for event in events
    )

    workspace = str(run.get("workspace_path") or "").strip()
    if workspace:
        try:
            if Path(workspace).expanduser().resolve() != root:
                return ()
        except OSError:
            return ()
    elif not relevant_edit:
        # Exact provenance can be user-forced. Without a workspace anchor, a
        # relevant edit is the minimum proof that this ledger belongs to the
        # reviewed change rather than an unrelated exact session id.
        return ()

    event_rows: list[ReviewJournalEntry] = []
    for raw in events[-_MAX_EVENT_ROWS:]:
        item = _event_entry(
            raw,
            host=host,
            session_id=session_id,
            repo_root=root,
            reviewed=reviewed,
        )
        if item is not None:
            event_rows.append(item)
    return _dedupe([*event_rows, *_state_entries(run, host=host, session_id=session_id)])


def _unavailable_journal(*, session_id: str, host: str, reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "session_id": session_id,
        "host": host,
        "task": "",
        "status": "",
        "updated_at": "",
        "plan": [],
        "verified_facts": [],
        "hypotheses_tried": [],
        "hypotheses_rejected": [],
        "open_questions": [],
        "blockers": [],
        "next_validation": "",
        "workflow_state": {},
        "plan_review": {},
        "task_progress": {},
        "tools_called": [],
        "commands_run": [],
        "tests_run": [],
        "files_touched": [],
        "activity": [],
    }


def _journal_path_matches(raw: str, changed_paths: frozenset[str]) -> str:
    candidate = _normalize_path(raw).lstrip("./")
    if not candidate:
        return ""
    if candidate in changed_paths:
        return candidate
    hits = [path for path in changed_paths if candidate.endswith("/" + path)]
    return hits[0] if len(hits) == 1 else ""


def _journal_activity(
    run: Mapping[str, Any],
    *,
    changed_paths: frozenset[str],
) -> list[dict[str, str]]:
    raw_events = run.get("events") or ()
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        return []
    rows: list[dict[str, str]] = []
    for raw in list(raw_events)[-_MAX_EVENT_ROWS:]:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "").strip()
        # Never surface model-private reasoning, checkpoints, or raw LLM-call
        # prompt/response payloads. The latter are recorded as tool_call rows
        # with payload.kind == "llm_call".
        if kind in {"reasoning", "checkpoint", "rubric_run", "watchdog_alert"}:
            continue
        payload = _event_payload(raw)
        if kind == "tool_call" and str(payload.get("kind") or "") == "llm_call":
            continue
        at = _bounded(raw.get("at"), 80)
        summary = _bounded(raw.get("summary"), _MAX_SUMMARY)
        if not summary:
            continue
        row = {"kind": kind, "summary": summary, "path": "", "status": "", "detail": "", "at": at}

        if kind in {"file_edit", "file_revert"}:
            path = _journal_path_matches(str(payload.get("path") or ""), changed_paths)
            if not path:
                continue
            row["path"] = path
            row["status"] = "CHANGED" if kind == "file_edit" else "REVERTED"
        elif kind in {"command", "command_result"}:
            status = _status_for_command(payload)
            row["status"] = status.upper()
            row["detail"] = _bounded(payload.get("error_signature"), _MAX_DETAIL)
        elif kind in {"test_result", "validation"}:
            passed = payload.get("passed")
            status = "pass" if passed is True else "fail" if passed is False else _status_for_command(payload)
            row["status"] = status.upper()
            row["detail"] = _bounded(
                payload.get("detail") or payload.get("error") or payload.get("message"),
                _MAX_DETAIL,
            )
        elif kind in {"tool_call", "tool_result"}:
            serial = json.dumps(payload, ensure_ascii=False, default=str)
            path = next((item for item in changed_paths if item in serial), "")
            if not path:
                continue
            row["path"] = path
            row["summary"] = _bounded(payload.get("tool") or payload.get("name") or summary, _MAX_SUMMARY)
        elif kind == "agent_message":
            role = str(payload.get("role") or "").strip().lower()
            if role not in {"user", "assistant", "agent"}:
                continue
            # Prompt/message text is already a user-/agent-visible conversation
            # surface, unlike private reasoning. Still bound it aggressively.
            text = (
                payload.get("prompt")
                if role == "user"
                else payload.get("message") or payload.get("text") or payload.get("response")
            )
            row["summary"] = _bounded(text or summary, _MAX_SUMMARY)
        elif kind in {"route_decision", "model_recommendation", "note"}:
            if kind == "note" and not summary.startswith(
                ("workflow:", "plan_review:", "task_progress:", "workflow_step:")
            ):
                continue
        else:
            continue
        rows.append(row)
    return rows


def load_review_journal(
    store_root: Path | str,
    *,
    session_id: str,
    host: str,
    changed_paths: Sequence[str],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Load the rich, bounded observable journal for one exact host session.

    This is a local author-side projection. It is safe to serialize into Review
    capture, but the server must never use it to guess author identity: the
    caller supplies the exact provenance host/session already established by the
    Review packet.
    """

    root = Path(store_root).expanduser().resolve()
    sid = str(session_id or "").strip()
    host_name = str(host or "").strip().lower()
    if not sid or not host_name:
        return _unavailable_journal(session_id=sid, host=host_name, reason="exact host/session identity is unavailable")

    found = find_session_dir(root, sid)
    if found is not None and found.parent.name != host_name:
        return _unavailable_journal(
            session_id=sid,
            host=host_name,
            reason=f"exact session belongs to host {found.parent.name}, not {host_name}",
        )
    run_path = (found / "run.json") if found is not None else session_dir(root, host_name, sid) / "run.json"
    if not run_path.is_file():
        return _unavailable_journal(
            session_id=sid, host=host_name, reason="exact author-session run ledger is unavailable"
        )
    run = _read_run(run_path)
    if run is None:
        return _unavailable_journal(
            session_id=sid, host=host_name, reason="exact author-session run ledger is unreadable"
        )

    recorded_agent = str(run.get("agent") or "").strip().lower()
    if recorded_agent and recorded_agent != host_name:
        return _unavailable_journal(
            session_id=sid,
            host=host_name,
            reason=f"run ledger host {recorded_agent} does not match exact host {host_name}",
        )

    workspace = str(run.get("workspace_path") or "").strip()
    if repo_root is not None and workspace:
        try:
            if Path(workspace).expanduser().resolve() != repo_root.expanduser().resolve():
                return _unavailable_journal(
                    session_id=sid,
                    host=host_name,
                    reason="exact author session belongs to a different workspace",
                )
        except OSError:
            return _unavailable_journal(
                session_id=sid, host=host_name, reason="exact author-session workspace is unavailable"
            )

    reviewed = frozenset(_normalize_path(path).lstrip("./") for path in changed_paths if _normalize_path(path))
    activity = _journal_activity(run, changed_paths=reviewed)
    # Snapshot state (plan/facts/questions/blockers/next validation) is just as
    # review-relevant as event rows. Project it into the compatibility activity
    # stream too so live and frozen journal consumers see one coherent feed.
    for item in _state_entries(run, host=host_name, session_id=sid):
        activity.append(
            {
                "id": item.id,
                "kind": item.kind,
                "summary": item.summary,
                "path": item.path,
                "status": item.status.upper(),
                "detail": item.detail,
                "at": item.at,
                "source": item.source,
            }
        )
    touched = [
        _journal_path_matches(str(item), reviewed) for item in (run.get("files_touched") or ()) if str(item).strip()
    ]
    touched = list(dict.fromkeys(path for path in touched if path))

    def strings(name: str, cap: int = _MAX_STATE_ROWS) -> list[str]:
        raw = run.get(name) or ()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            return []
        return [_bounded(item, _MAX_SUMMARY) for item in list(raw)[:cap] if str(item).strip()]

    return {
        "available": True,
        "reason": "",
        "session_id": sid,
        "host": host_name,
        "task": _bounded(run.get("task"), _MAX_SUMMARY),
        "status": _bounded(run.get("status"), 64),
        "updated_at": _bounded(run.get("updated_at"), 80),
        "plan": strings("current_plan", _MAX_PLAN_STEPS),
        "verified_facts": strings("verified_facts"),
        "hypotheses_tried": strings("hypotheses_tried"),
        "hypotheses_rejected": strings("hypotheses_rejected"),
        "open_questions": strings("open_questions"),
        "blockers": strings("current_blockers"),
        "next_validation": _bounded(run.get("next_required_validation"), _MAX_SUMMARY),
        "workflow_state": (
            dict(run.get("workflow_state") or {}) if isinstance(run.get("workflow_state"), Mapping) else {}
        ),
        "plan_review": dict(run.get("plan_review") or {}) if isinstance(run.get("plan_review"), Mapping) else {},
        "task_progress": dict(run.get("task_progress") or {}) if isinstance(run.get("task_progress"), Mapping) else {},
        "tools_called": strings("tools_called"),
        "commands_run": strings("commands_run"),
        "tests_run": strings("tests_run"),
        "files_touched": touched,
        "activity": activity,
    }


__all__ = ["collect_review_journal", "load_review_journal"]
