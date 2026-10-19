import { afterEach, describe, expect, it, vi } from "vitest";

import { openReviewTargetInEditor, reviewEditorLocation } from "./reviewEditor";
import type { ReviewTarget } from "./types";

function target(overrides: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: "t1",
    unit_key: "u1",
    kind: "symbol",
    path: "src/app.py",
    label: "alpha",
    symbol: "alpha",
    start_line: 10,
    end_line: 20,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: 12, end_line: 14, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 0,
    fingerprint_method: "",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    ...overrides,
  };
}

describe("review editor location", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("opens the first changed new-side line", () => {
    expect(reviewEditorLocation(target())).toEqual({ path: "src/app.py", line: 12 });
  });

  it("falls back to the target start line without new-side spans", () => {
    expect(reviewEditorLocation(target({ spans: [] }))).toEqual({ path: "src/app.py", line: 10 });
  });

  it("has no workspace location for a pure deletion", () => {
    const deleted = target({
      start_line: 0,
      spans: [{ side: "old", start_line: 3, end_line: 9, hunk_ordinal: 0 }],
    });
    expect(reviewEditorLocation(deleted)).toBeNull();
    expect(reviewEditorLocation(null)).toBeNull();
  });

  it("posts the exact repository-relative location to the shared editor endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ opened: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await openReviewTargetInEditor("/repo", { path: "src/app.py", line: 12 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/code-map/editor");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ project_root: "/repo", path: "src/app.py", line: 12, column: 1 });
  });
});
