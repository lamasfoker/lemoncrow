"""The agent host's supervision hook for risky actions (Phase 12 item 14).

Claude Code calls this before a Bash command or a file write/edit
(``PreToolUse``). An action outside every configured class passes through
untouched (no output, so the host's own permissions apply). An action in a
configured class files an approval request with the Review, waits a bounded
time for a human decision, and then:

* approved (and claimed for this exact action) -> passes through to the host's
  normal permission flow; it is never force-allowed, so a host rule that would
  have asked or denied still does;
* denied, expired, timed out, ambiguous, or the service unreachable -> denied
  with a clear reason (**fail closed**).

The Review is ``LEMONCROW_REVIEW_ID`` when set; otherwise, on a local install,
the single open Review whose worktree is this checkout. Anything else is refused
with guidance rather than guessed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote

from lemoncrow.pro.capabilities.review.action_approval import (
    RISK_LABELS,
    ApprovalConfigError,
    ApprovalPolicy,
    classify_action,
    read_policy,
    unclassifiable_parts,
)

HOST: Final[str] = "claude"
POLL_SECONDS: Final[float] = 2.0
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
_COMMAND_TOOLS: Final[frozenset[str]] = frozenset({"Bash"})
_WRITE_TOOLS: Final[dict[str, str]] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
GATED_TOOLS: Final[frozenset[str]] = _COMMAND_TOOLS | frozenset(_WRITE_TOOLS)


class ServiceUnavailable(RuntimeError):
    """The Review decision service could not be reached or refused the request."""


class ApprovalClient(Protocol):
    def reviews(self) -> list[Mapping[str, Any]]: ...

    def request(self, review_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def claim(self, review_id: str, approval_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HookOutcome:
    decision: Literal["pass", "deny"]
    reason: str = ""
    message: str = ""

    def render(self) -> str:
        """The hook's stdout: nothing for a plain pass, a deny decision, or a notice."""

        if self.decision == "deny":
            return json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": self.reason,
                    }
                }
            )
        return json.dumps({"systemMessage": self.message}) if self.message else ""


class HttpApprovalClient:
    """Bearer-token JSON client for the Review Reader API (local or hosted)."""

    def __init__(self, url: str, token: str, *, timeout_s: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_s

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(dict(body)).encode("utf-8")
        request = urllib.request.Request(f"{self._url}{path}", data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
                detail = str(payload.get("detail") or payload.get("message") or "")
            except (ValueError, AttributeError, OSError):
                detail = ""
            raise ServiceUnavailable(
                f"the Review service answered {exc.code}{': ' + detail if detail else ''}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ServiceUnavailable(f"the Review service at {self._url} could not be reached") from exc

    def reviews(self) -> list[Mapping[str, Any]]:
        payload = self._call("GET", "/api/reviews")
        rows = payload.get("reviews") if isinstance(payload, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def request(self, review_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self._call("POST", f"/api/reviews/{quote(review_id, safe='')}/agent-approvals", body)
        if not isinstance(payload, dict):
            raise ServiceUnavailable("the Review service returned an unexpected answer")
        return payload

    def claim(self, review_id: str, approval_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        path = f"/api/reviews/{quote(review_id, safe='')}/agent-approvals/{quote(approval_id, safe='')}/claim"
        payload = self._call("POST", path, body)
        if not isinstance(payload, dict):
            raise ServiceUnavailable("the Review service returned an unexpected answer")
        return payload


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def tool_action(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(kind, action)`` for a gated tool call, or ``None`` for any other tool."""

    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping) or tool not in GATED_TOOLS:
        return None
    if tool in _COMMAND_TOOLS:
        return "command", str(tool_input.get("command") or "")
    path = str(tool_input.get(_WRITE_TOOLS[tool]) or "").strip()
    cwd = str(payload.get("cwd") or "")
    if path and not path.startswith("/") and cwd:
        # Joined, never normalized: ``..`` stays visible to the classifier and the human.
        path = os.path.join(cwd, path)
    return "write", path


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _labels(classes: frozenset[str]) -> str:
    return ", ".join(RISK_LABELS.get(name, name) for name in sorted(classes))


def resolve_review(
    client: ApprovalClient, *, env: Mapping[str, str], cwd: str, local: bool, git: Callable[..., str] = _git
) -> str:
    """The Review to ask, or :class:`LookupError` with what the developer can do."""

    explicit = str(env.get("LEMONCROW_REVIEW_ID") or "").strip()
    if explicit:
        return explicit
    if not local:
        raise LookupError("set LEMONCROW_REVIEW_ID to the hosted Review this agent works on")
    toplevel = git(cwd, "rev-parse", "--show-toplevel")
    if not toplevel:
        raise LookupError("this agent is not working in a Git checkout with a LemonCrow Review")
    root = str(Path(toplevel).resolve())
    matches = [
        str(row.get("id") or "")
        for row in client.reviews()
        if row.get("status") == "open" and str(row.get("worktree_root") or "") == root and row.get("id")
    ]
    if not matches:
        raise LookupError(f"no open LemonCrow Review for {root}; run `lc review` there or set LEMONCROW_REVIEW_ID")
    if len(matches) > 1:
        shown = ", ".join(matches[:5])
        raise LookupError(f"{len(matches)} open Reviews use {root} ({shown}); set LEMONCROW_REVIEW_ID to choose one")
    return matches[0]


def run_gate(
    payload: Mapping[str, Any],
    *,
    policy: Callable[[], ApprovalPolicy],
    client: Callable[[], tuple[ApprovalClient, bool]],
    env: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    git: Callable[..., str] = _git,
) -> HookOutcome:
    """Decide one tool call. ``client()`` returns ``(client, is_local_install)``."""

    found = tool_action(payload)
    if found is None:
        return HookOutcome("pass")
    kind, action = found
    cwd = str(payload.get("cwd") or "")
    repo_root = git(cwd, "rev-parse", "--show-toplevel") if cwd else ""
    classes = classify_action(kind, action, repo_root=repo_root or cwd)
    if not classes:
        return HookOutcome("pass")
    try:
        configured = policy()
    except ApprovalConfigError as exc:
        return HookOutcome(
            "deny",
            f"LemonCrow approval settings could not be read ({exc}); this {_labels(classes).lower()} "
            "action is blocked until they can (fail closed).",
        )
    required = configured.required(classes)
    if not required:
        return HookOutcome("pass")  # unconfigured classes are unaffected
    label = _labels(required)
    blocked = f"LemonCrow requires a human decision for: {label}."
    unreadable = unclassifiable_parts(kind, action)
    if unreadable:
        # Fail closed: what runs is decided only at run time, so it may be any class.
        blocked += f" Its effect cannot be read from its text ({'; '.join(unreadable[:2])})."
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return HookOutcome("deny", f"{blocked} The agent session is unknown, so no approval can be requested.")
    try:
        service, local = client()
        review_id = resolve_review(service, env=env, cwd=cwd, local=local, git=git)
    except LookupError as exc:
        return HookOutcome("deny", f"{blocked} No Review to ask: {exc}. Blocked (fail closed).")
    except (ServiceUnavailable, ValueError, OSError) as exc:
        return HookOutcome("deny", f"{blocked} {exc}; the action is blocked (fail closed).")
    identity = {"host": HOST, "session_id": session_id, "kind": kind, "action": action, "cwd": cwd}
    try:
        filed = service.request(review_id, {**identity, "classes": sorted(required)})
    except ServiceUnavailable as exc:
        return HookOutcome("deny", f"{blocked} {exc}; the action is blocked (fail closed).")
    approval_id = str(_mapping(filed.get("approval")).get("id") or "")
    if not approval_id:
        return HookOutcome("deny", f"{blocked} The Review did not accept the request; blocked (fail closed).")
    deadline = clock() + configured.wait_seconds
    last_error = ""
    while True:
        try:
            answer = service.claim(review_id, approval_id, identity)
            last_error = ""
        except ServiceUnavailable as exc:
            answer = {}
            last_error = str(exc)
        current = _mapping(answer.get("approval"))
        if answer.get("granted") is True:
            who = str(current.get("decided_by") or "a reviewer")
            return HookOutcome("pass", message=f"LemonCrow: {label} approved by {who} in Review {review_id}.")
        reason = str(answer.get("reason") or "")
        if answer and reason != "pending":
            if reason == "denied":
                who = str(current.get("decided_by") or "a reviewer")
                note = str(current.get("note") or "").strip()
                return HookOutcome(
                    "deny", f"Denied by {who} in LemonCrow Review {review_id}{': ' + note if note else ''}."
                )
            return HookOutcome("deny", f"{blocked} Not approved ({reason or 'no decision'}); blocked.")
        if clock() >= deadline:
            if last_error:
                return HookOutcome("deny", f"{blocked} {last_error}; the action is blocked (fail closed).")
            return HookOutcome(
                "deny",
                f"{blocked} No decision within {configured.wait_seconds}s. Approve request {approval_id} "
                f"in LemonCrow Review {review_id}, then retry the action.",
            )
        sleep(POLL_SECONDS)


def _default_client() -> tuple[ApprovalClient, bool]:
    from lemoncrow_client.config import load_config

    config = load_config(product="review")
    return HttpApprovalClient(config.url, config.token), not config.hosted


def _default_policy() -> ApprovalPolicy:
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    return read_policy(settings_root())


def main_from_text(raw: str, *, env: Mapping[str, str] | None = None) -> str:
    """Hook entry: stdin text in, stdout text out. Never raises for a gated tool."""

    environment = dict(os.environ) if env is None else dict(env)
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return ""
    try:
        return run_gate(payload, policy=_default_policy, client=_default_client, env=environment).render()
    except Exception as exc:  # the gate itself failed: configured classes fail closed
        if tool_action(payload) is None:
            return ""
        try:
            any_configured = bool(_default_policy().classes)
        except Exception:
            any_configured = True  # unreadable settings: assume configured
        if not any_configured:
            return ""
        return HookOutcome(
            "deny",
            f"LemonCrow's approval gate failed ({type(exc).__name__}); this action is blocked (fail closed).",
        ).render()


__all__ = [
    "GATED_TOOLS",
    "HookOutcome",
    "HttpApprovalClient",
    "ServiceUnavailable",
    "main_from_text",
    "resolve_review",
    "run_gate",
    "tool_action",
]
