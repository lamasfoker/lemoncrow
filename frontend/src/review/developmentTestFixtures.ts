/** Development projection fixtures shared by the Development sheet and drawer tests. */

import type { DevelopmentEvent, DevelopmentProjection, ReviewDevelopment } from "./types";

export function projection(overrides: Partial<DevelopmentProjection> = {}): DevelopmentProjection {
  return {
    scope: "snapshot",
    source: "events",
    available: true,
    headline: "Session expiry now rejects stale tokens; focused auth tests pass.",
    intent: "Harden session expiry",
    plan: null,
    contributors: [
      {
        key: "claude:c1", host: "claude", session_id: "c1", model: "claude-opus", purpose: "implement", parent_session_id: "",
        role: "author", paths: ["src/auth.py", "src/session.py"], path_count: 2, edit_count: 4,
        checks: { pass: 2, fail: 0, pending: 0 }, first_at: "", last_at: "", status: "done", task: "", intent: "",
        latest_message: "", active: false, evidence: "events",
      },
      {
        key: "codex:x1", host: "codex", session_id: "x1", model: "gpt-5", purpose: "implement", parent_session_id: "",
        role: "author", paths: ["src/cache.py"], path_count: 1, edit_count: 1,
        checks: { pass: 0, fail: 0, pending: 0 }, first_at: "", last_at: "", status: "", task: "", intent: "",
        latest_message: "", active: false, evidence: "events",
      },
      {
        key: "ci:v1", host: "ci", session_id: "v1", model: "", purpose: "verify", parent_session_id: "",
        role: "verifier", paths: [], path_count: 0, edit_count: 0,
        checks: { pass: 40, fail: 1, pending: 0 }, first_at: "", last_at: "", status: "", task: "", intent: "",
        latest_message: "", active: false, evidence: "events",
      },
    ],
    counts: { authors: 2, verifiers: 1, investigators: 0, files: 3, decisions: 1, open_items: 1 },
    decisions: [{
      id: "d1", sequence: 4, kind: "decision", summary: "Rejected: cache session tokens", detail: "", status: "", path: "",
      host: "claude", session_id: "c1", role: "author", at: "", causation_id: "", annotation_id: "",
    }],
    open_items: [{
      id: "q1", sequence: 5, kind: "question", summary: "Should mobile keep legacy cookies?", detail: "", status: "open", path: "",
      host: "claude", session_id: "c1", role: "author", at: "", causation_id: "", annotation_id: "",
    }],
    verification: {
      pass: 42, fail: 1, pending: 0,
      checks: [{ name: "mypy", summary: "uv run mypy src", kind: "validation", status: "fail", detail: "2 errors", host: "ci", session_id: "v1", at: "", sequence: 9 }],
    },
    changed_paths: [
      { path: "src/auth.py", status: "changed", sessions: ["claude:c1"], edits: 3, last_at: "", in_snapshot: true },
      { path: "src/cache.py", status: "changed", sessions: ["codex:x1", "claude:c1"], edits: 1, last_at: "", in_snapshot: true },
    ],
    unattributed_paths: ["docs/notes.md"],
    feedback: [{
      causation_id: "feedback:op-1", annotation_ids: ["ann-1", "ann-2"], sessions: ["claude:c1"], summary: "", handoff_status: "sent",
      edit_count: 2, paths: ["src/auth.py"], checks: { pass: 4, fail: 0 }, first_at: "", last_at: "", included_in_revision: 53,
    }],
    latest_event: null,
    event_count: 12,
    cursor: 12,
    ...overrides,
  };
}

export function development(overrides: Partial<ReviewDevelopment> = {}): ReviewDevelopment {
  return {
    review_id: "review-1",
    revision_id: "rrv-52",
    revision_number: 52,
    historical: false,
    live: true,
    cursor: "12",
    snapshot: projection(),
    since: null,
    reason: "",
    ...overrides,
  };
}

export function event(id: string, overrides: Partial<DevelopmentEvent> = {}): DevelopmentEvent {
  return {
    id, sequence: Number(id.replace(/\D/g, "")) || 1, kind: "edit", at: "2026-09-27T10:00:00+00:00", host: "claude",
    session_id: "c1", role: "author", path: "src/auth.py", symbol: "", target_unit_key: "", summary: "Edited src/auth.py",
    detail: "", status: "changed", source: "run_ledger", causation_id: "", parent_event_id: "", annotation_id: "", task_id: "",
    ...overrides,
  };
}
