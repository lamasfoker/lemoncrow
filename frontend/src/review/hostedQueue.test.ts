import { describe, expect, it } from "vitest";

import { hostedActionSummary, isQueuedWrite, queuedWriteNotice, unresolvedHostedActions } from "./hostedQueue";
import type { HostedAction, HostedQueuedWrite } from "./types";

function action(overrides: Partial<HostedAction>): HostedAction {
  return {
    id: "rha-1",
    sequence: 1,
    kind: "mark",
    snapshot_id: "rrv-h1",
    supersede_key: "",
    payload: {},
    preconditions: {},
    state: "pending",
    reason: "",
    result: {},
    created_at: "",
    resolved_at: "",
    ...overrides,
  };
}

describe("hostedQueue", () => {
  it("recognises only an explicit queued answer", () => {
    expect(isQueuedWrite({ queued: true, reference: "r/9", actions: [] })).toBe(true);
    for (const value of [null, undefined, "queued", { queued: "true" }, { queued: 1 }, { target_id: "t", state: "reviewed" }]) {
      expect(isQueuedWrite(value)).toBe(false);
    }
  });

  it("names each queued action from its own payload", () => {
    expect(hostedActionSummary(action({ payload: { state: "reviewed", unit_key: "sym:a" } }))).toBe("Mark reviewed · sym:a");
    expect(hostedActionSummary(action({ payload: { state: "unreviewed", unit_key: "sym:a" } }))).toBe("Clear judgment · sym:a");
    expect(hostedActionSummary(action({ kind: "comment", payload: { path: "src/a.py", start_line: 4, body: "  why\n this? " } })))
      .toBe("Comment on src/a.py:L4: “why this?”");
    expect(hostedActionSummary(action({ kind: "comment", payload: { path: "src/a.py", start_line: 0, body: "x" } })))
      .toBe("Comment on src/a.py: “x”");
    expect(hostedActionSummary(action({ kind: "reply", payload: { body: "y".repeat(80) } }))).toBe(`Reply: “${"y".repeat(59)}…”`);
    expect(hostedActionSummary(action({ kind: "outcome", payload: { outcome: "changes_requested" } }))).toBe("Verdict changes requested");
    expect(hostedActionSummary(action({ kind: "request_complete" }))).toBe("Complete request");
  });

  it("acknowledges a queued write with the hosted reference and the sync step", () => {
    const queued: HostedQueuedWrite = {
      queued: true,
      reference: "r/9",
      actions: [action({ payload: { state: "reviewed", unit_key: "sym:a" } })],
      superseded: [],
      counts: { pending: 1, applied: 0, conflict: 0, discarded: 0 },
    };
    expect(queuedWriteNotice(queued)).toBe("Queued for hosted r/9: Mark reviewed · sym:a. It applies when you run `lc review hosted sync`.");
  });

  it("lists only unresolved actions, conflicts first, each in queue order", () => {
    const listed = unresolvedHostedActions({
      attached: true,
      review_id: "rv",
      actions: [
        action({ id: "a", sequence: 1, state: "pending" }),
        action({ id: "b", sequence: 2, state: "applied" }),
        action({ id: "c", sequence: 3, state: "conflict" }),
        action({ id: "d", sequence: 4, state: "discarded" }),
        action({ id: "e", sequence: 5, state: "pending" }),
        action({ id: "f", sequence: 0, state: "conflict" }),
      ],
    });
    expect(listed.map((item) => item.id)).toEqual(["f", "c", "a", "e"]);
    expect(unresolvedHostedActions(null)).toEqual([]);
  });
});
