import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewHistorySheet from "./ReviewHistorySheet";

const api = vi.hoisted(() => ({
  fetchRevisions: vi.fn(),
  fetchReviewActivity: vi.fn(),
  fetchFixLines: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  fetchRevisions: api.fetchRevisions,
  fetchReviewActivity: api.fetchReviewActivity,
  fetchFixLines: api.fetchFixLines,
  startFixLine: vi.fn(),
  planFixApply: vi.fn(),
  applyFixLine: vi.fn(),
  discardFixLine: vi.fn(),
  resolveFixLine: vi.fn(),
  recreateFixWorktree: vi.fn(),
  historicalReviewHref: (reviewId: string, revisionNumber: number) => `/r/${reviewId}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (reviewId: string, base: number, target: number) => `/r/${reviewId}?base=${base}&target=${target}`,
  sourceCompareHref: (from: string, to: string, scope: string) => `/r/x?from=${from}&to=${to}&scope=${scope}`,
}));

describe("ReviewHistorySheet", () => {
  beforeEach(() => {
    api.fetchFixLines.mockResolvedValue({ fix_lines: [], fix_of: null });
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { id: "11111111111171118111111111111111", ref: "rr/11111111111171118111111111111111", revision_number: 0, range_mode: "working_tree", base_sha: "a", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T18:00:00Z", is_last_reviewed: true, is_last_commented: true, previous_revision_number: null, review_area_count: 2, review_area_delta: { added: 2, changed: 0, removed: 0 }, delta: { added: 3, changed: 0, removed: 0 } },
        { id: "22222222222272228222222222222222", ref: "rr/22222222222272228222222222222222", revision_number: 1, range_mode: "working_tree", base_sha: "b", head_sha: "abcdef1234567890", title: "Implemented safer price validation", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T19:00:00Z", is_latest: true, previous_revision_number: 0, semantic_changed: false, source_identity_changed: true, base_moved: true, review_area_count: 2, review_area_delta: { added: 0, changed: 0, removed: 0 }, delta: { added: 0, changed: 0, removed: 0 } },
      ],
    });
    api.fetchReviewActivity.mockResolvedValue({
      review_id: "rev-abc",
      events: [
        {
          id: "act-system",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "mark.reconciled",
          actor_id: "lemoncrow",
          actor_type: "unknown",
          subject_type: "review_target",
          subject_id: "sym:checkout",
          summary: "local: reviewed → changed since review",
          detail: { reason: "content changed" },
          created_at: "2026-09-15T19:00:01Z",
        },
        {
          id: "act-human",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "mark.judgment",
          actor_id: "pankaj",
          actor_type: "human",
          subject_type: "review_target",
          subject_id: "sym:checkout",
          summary: "pankaj: changed since review → needs changes",
          detail: {},
          created_at: "2026-09-15T19:01:00Z",
        },
        {
          id: "act-agent",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "annotation.author_response",
          actor_id: "claude-session",
          actor_type: "agent",
          subject_type: "annotation",
          subject_id: "ann-1",
          summary: "Author says addressed",
          detail: {},
          created_at: "2026-09-15T19:02:00Z",
        },
      ],
    });
  });

  it("shows an unfrozen legacy Review base without inventing a recorded SHA", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} rangeMode="working_tree" onClose={vi.fn()} />);
    expect(await screen.findByTestId("history-review-base")).toHaveTextContent("Base not frozen");
    expect(screen.getByText("Snapshot base b")).toBeTruthy();
    expect(screen.queryByText(/Review base [a-f0-9]{6}/)).toBeNull();
  });

  it("shows the frozen Review base apart from each Snapshot's effective base", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} rangeMode="staged" reviewBaseSha="0123456789abcdef" onClose={vi.fn()} />);
    expect(await screen.findByTestId("history-review-base")).toHaveTextContent("Review base 0123456789ab");
    expect(screen.getByText("Snapshot base a")).toBeTruthy();
    expect(screen.getByText("Snapshot base b")).toBeTruthy();
    expect(screen.getByText("Base moved · review areas unchanged")).toBeTruthy();
  });

  it("opens on revisions and keeps append-only activity one tab away", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0));
    expect(screen.getByRole("radio", { name: /snapshots/i }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("2 initial review areas")).toBeTruthy();
    expect(screen.getByText("Δ +0 ~0 −0 review areas")).toBeTruthy();
    expect(screen.queryByText("3 initial review areas")).toBeNull();
    expect(screen.getByText("Base moved · review areas unchanged")).toBeTruthy();
    expect(screen.getByText("Provider abcdef1234")).toBeTruthy();
    expect(screen.getByText("Implemented safer price validation")).toBeTruthy();
    expect(screen.queryByRole("radio", { name: /compare/i })).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /activity/i }));
    await waitFor(() => expect(screen.getByText("Author says addressed")).toBeTruthy());
    expect(screen.getByText("pankaj")).toBeTruthy();
    expect(screen.getByText("claude-session")).toBeTruthy();
    expect(screen.getByText("lemoncrow")).toBeTruthy();
    expect(screen.getByText("content changed")).toBeTruthy();
  });

  it("shows a deployment outcome as an agent activity line naming the commit", async () => {
    api.fetchReviewActivity.mockResolvedValueOnce({
      review_id: "rev-abc",
      events: [
        {
          id: "act-deploy",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "deployment.observed",
          actor_id: "provider_github",
          actor_type: "agent",
          subject_type: "deployment",
          subject_id: "deployment:production",
          summary: "Deployed to production canary 10% (eeeeeeeeeeee contains this change).",
          detail: { environment: "production", state: "deployed" },
          created_at: "2026-10-04T09:00:00Z",
        },
      ],
    });
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0));
    await userEvent.click(screen.getByRole("radio", { name: /activity/i }));
    expect(
      await screen.findByText("Deployed to production canary 10% (eeeeeeeeeeee contains this change)."),
    ).toBeTruthy();
    expect(screen.getByText("deployment.observed")).toBeTruthy();
    expect(screen.getByText("provider_github")).toBeTruthy();
  });

  it("offers a retry when history cannot be loaded", async () => {
    api.fetchRevisions.mockRejectedValueOnce(new Error("history unavailable"));

    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("Could not load history")).toBeTruthy();
    expect(screen.getByText("history unavailable")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0));
    expect(api.fetchRevisions.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("keeps the failure and a usable Retry when activity fails and the retry fails too", async () => {
    api.fetchReviewActivity
      .mockRejectedValueOnce(new Error("activity unavailable"))
      .mockRejectedValueOnce(new Error("activity still unavailable"));

    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("activity unavailable")).toBeTruthy();
    expect(screen.queryByText("No persisted Snapshots.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("activity still unavailable")).toBeTruthy();
    expect(screen.getByText("Could not load history")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText("Could not load history")).toBeNull());
    expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0);
  });

  it("keeps quick and arbitrary revision comparison inside history", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={0} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText("Snapshot 0").length).toBeGreaterThan(0));

    const quickCompare = screen.getByRole("link", { name: /compare to latest/i });
    expect(quickCompare.getAttribute("href")).toBe("/r/rev-abc?base=0&target=1");

    await userEvent.click(screen.getByText("Advanced comparison"));
    expect(screen.getByLabelText("Compare from Snapshot")).toBeTruthy();
    expect(screen.getByLabelText("Compare to Snapshot")).toBeTruthy();
    const advancedCompare = screen.getByRole("link", { name: "Compare" });
    expect(advancedCompare.getAttribute("href")).toBe("/r/rev-abc?base=0&target=1");
  });
  it("keeps every captured Snapshot visible, emphasizes feedback, and links arbitrary Git comparisons", async () => {
    api.fetchRevisions.mockResolvedValueOnce({
      revisions: [
        { id: "feedback", ref: "rr/feedback", revision_number: 0, capture_kind: "draft", has_review_feedback: true, is_last_commented: true },
        { id: "quiet", ref: "rr/quiet", revision_number: 1, capture_kind: "draft" },
        { id: "current", ref: "rr/current", revision_number: 2, capture_kind: "draft", is_latest: true, base_sha: "abc" },
      ],
    });
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={2} historical={false} onClose={vi.fn()} />);
    expect(await screen.findByRole("link", { name: "Open Draft capture 0" })).toBeTruthy();
    expect(screen.getByText("Latest feedback")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Draft capture 1" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Draft capture 2" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Compare Git commits and Snapshots" }).getAttribute("href"))
      .toBe("/r/x?from=git/abc&to=rr/current&scope=r/rev-abc");
    await userEvent.click(screen.getByText("Advanced comparison"));
    expect(screen.getByRole("link", { name: "Compare" }).getAttribute("href")).toBe("/r/rev-abc?base=0&target=2");
  });

  it("links the latest revision and older revisions through human revision numbers", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={0} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("radio", { name: /snapshots/i })).toBeTruthy());
    const latest = screen.getAllByText("Snapshot 1").map((node) => node.closest("a")).find(Boolean);
    const old = screen.getAllByText("Snapshot 0").map((node) => node.closest("a")).find(Boolean);
    expect(latest?.getAttribute("href")).toBe("/r/rev-abc?base=1&target=1");
    expect(old?.getAttribute("href")).toBe("/r/rev-abc?base=0&target=0");
    expect(screen.getByText("Newest saved")).toBeTruthy();
    expect(screen.getByText("Viewing")).toBeTruthy();
    expect(screen.queryByText(/checkpoint rrv-/)).toBeNull();
  });

  it("keeps ordinary draft captures, published captures, and commit checkpoints selectable in Snapshot history", async () => {
    api.fetchRevisions.mockResolvedValueOnce({
      revisions: [
        { id: "commit", revision_number: 4, capture_kind: "commit", commit_number: 2, commit_sha: "abcdef123456", created_at: "2026-09-15T18:00:00Z" },
        { id: "published", revision_number: 5, capture_kind: "draft", is_published_snapshot: true, dirty: true, created_at: "2026-09-15T19:00:00Z" },
        { id: "internal", revision_number: 6, capture_kind: "draft", dirty: true, created_at: "2026-09-15T19:01:00Z" },
      ],
    });
    api.fetchReviewActivity.mockResolvedValueOnce({ events: [
      { id: "internal-event", revision_id: "internal", kind: "revision.recorded", actor_type: "unknown", detail: {}, summary: "Internal draft" },
      { id: "saved-event", revision_id: "published", kind: "snapshot.published", actor_type: "unknown", detail: {}, summary: "Snapshot saved" },
    ] });

    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={6} historical={false} onClose={vi.fn()} />);
    const saved = await screen.findByRole("link", { name: "Open Snapshot 5" });
    expect(saved.getAttribute("href")).toBe("/r/rev-abc?base=5&target=5");
    expect(screen.getByRole("link", { name: "Open Snapshot 4" }).getAttribute("href")).toBe("/r/rev-abc?base=4&target=4");
    expect(screen.getByRole("link", { name: "Open Draft capture 6" }).getAttribute("href")).toBe("/r/rev-abc?base=6&target=6");
    expect(screen.getByRole("radio", { name: /snapshots/i }).textContent).toContain("3");
    expect(screen.getByText("Commit 2 · abcdef12")).toBeTruthy();
    await userEvent.click(screen.getByRole("radio", { name: /activity/i }));
    expect(screen.queryByText("Internal draft")).toBeNull();
    expect(screen.getByText("Snapshot saved")).toBeTruthy();
  });
  it("shows the running draft above history with the latest saved baseline and a pinned link", async () => {
    api.fetchRevisions.mockResolvedValueOnce({ revisions: [
      { id: "draft", revision_number: 8, capture_kind: "draft", dirty: true },
      { id: "saved", revision_number: 7, capture_kind: "draft", is_published_snapshot: true },
      { id: "commit", revision_number: 6, capture_kind: "commit" },
    ] });
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={7} historical onClose={vi.fn()} />);
    expect(await screen.findByText("Working changes since Snapshot 7. Read-only until you create a Snapshot.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open draft" }).getAttribute("href")).toBe("/r/rev-abc?base=8&target=8");
    expect(screen.getByText("Newest saved").closest("a")?.textContent).toContain("Snapshot 7");
    expect(screen.queryByText(/newest saved Snapshot 8/)).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /activity/i }));
    expect(screen.getByText("Working changes since Snapshot 7. Read-only until you create a Snapshot.")).toBeTruthy();
  });

  it("shows a first draft without inventing a saved baseline", async () => {
    api.fetchRevisions.mockResolvedValueOnce({ revisions: [
      { id: "draft", revision_number: 0, capture_kind: "draft" },
    ] });
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={0} historical={false} onClose={vi.fn()} />);
    expect(await screen.findByText("Working changes. No saved Snapshot yet.")).toBeTruthy();
    expect(screen.queryByText("Newest saved")).toBeNull();
  });

  it.each(["draft", "commit"])("does not show a running draft for a saved %s capture", async (captureKind) => {
    api.fetchRevisions.mockResolvedValueOnce({ revisions: [
      { id: "saved", revision_number: 2, capture_kind: captureKind, is_published_snapshot: true, dirty: true },
      { id: "older-draft", revision_number: 1, capture_kind: "draft" },
    ] });
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={2} historical={false} onClose={vi.fn()} />);
    await screen.findByRole("link", { name: "Open Snapshot 2" });
    expect(screen.queryByRole("link", { name: "Open draft" })).toBeNull();
    expect(screen.queryByText("Running draft")).toBeNull();
  });
});
