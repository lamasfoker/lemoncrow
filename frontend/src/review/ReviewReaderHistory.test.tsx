import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewReader from "./ReviewReader";

vi.mock("./reviewEnvironment", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./reviewEnvironment")>();
  const local = actual.fallbackReviewEnvironment("local");
  return {
    ...actual,
    useReviewEnvironment: () => ({
      ...local,
      discovered: true,
      review: { ...local.review, brief: false },
    }),
  };
});

const api = vi.hoisted(() => ({
  fetchHistoricalOverview: vi.fn(),
  fetchHistoricalTargets: vi.fn(),
  fetchHistoricalAnnotations: vi.fn(),
  fetchHistoricalFile: vi.fn(),
  fetchHistoricalReviewJournal: vi.fn(),
  fetchOverview: vi.fn(),
  fetchStartupOverview: vi.fn(),
  fetchReviewJournal: vi.fn(),
  fetchReviewDevelopment: vi.fn(),
  fetchHistoricalReviewDevelopment: vi.fn(),
  nextLiveState: vi.fn(),
  fetchTargets: vi.fn(),
  fetchStartupTargets: vi.fn(),
  fetchTargetsForPath: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchFile: vi.fn(),
  fetchRevisionLocator: vi.fn(),
  fetchRevisions: vi.fn(),
  fetchRevisionComparison: vi.fn(),
  fetchSourceComparison: vi.fn(),
  fetchSourceState: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchReviewReadiness: vi.fn(),
  armReviewLanding: vi.fn(),
  cancelReviewLanding: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchReviewSurfaces: vi.fn(),
  fetchChangeProposals: vi.fn(),
  fetchProposalSelection: vi.fn(),
  fetchReviewPreparation: vi.fn(),
  fetchReviewPatch: vi.fn(),
  deliverFeedback: vi.fn(),
  createChangeProposal: vi.fn(),
  applyChangeProposal: vi.fn(),
  postMark: vi.fn(),
  fetchFixLines: vi.fn(),
}));

vi.mock("./ReviewStream", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef((props: any, ref: any) => {
      React.useImperativeHandle(ref, () => ({
        scrollToTarget: vi.fn(),
        scrollToFile: vi.fn(),
        scrollToSurface: vi.fn(),
      }));
      return <div data-testid="historical-stream">{props.targets.length} historical target</div>;
    }),
  };
});

vi.mock("./reviewApi", () => ({
  authenticatedFetch: vi.fn(async () => Response.json({ notes: [] })),
  adoptBootstrapFragment: () => ({ token: "t", reviewId: "rev-history" }),
  selectReviewId: vi.fn(),
  reviewDirectoryHref: () => "/reviews#t=t",
  reviewHref: (id: string) => `/r/${id.startsWith("r/") ? id.slice(2) : id}`,
  historicalReviewHref: (id: string, revisionNumber?: number) => revisionNumber === undefined
    ? `/rr/${id.startsWith("rr/") ? id.slice(3) : id}`
    : `/r/${id.startsWith("r/") ? id.slice(2) : id}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (id: string, base: number, target: number) => `/r/${id.startsWith("r/") ? id.slice(2) : id}?base=${base}&target=${target}`,
  sourceCompareHref: (from: string, to: string) => `/r/x/${from}..${to}`,
  fetchHistoricalOverview: api.fetchHistoricalOverview,
  fetchHistoricalTargets: api.fetchHistoricalTargets,
  fetchHistoricalAnnotations: api.fetchHistoricalAnnotations,
  fetchHistoricalFile: api.fetchHistoricalFile,
  fetchHistoricalReviewJournal: api.fetchHistoricalReviewJournal,
  fetchOverview: api.fetchOverview,
  fetchStartupOverview: api.fetchStartupOverview,
  fetchReviewJournal: api.fetchReviewJournal,
  fetchReviewDevelopment: api.fetchReviewDevelopment,
  fetchHistoricalReviewDevelopment: api.fetchHistoricalReviewDevelopment,
  fetchReviewDevelopmentEvents: vi.fn(async () => ({ events: [], next_before: null })),
  nextLiveState: api.nextLiveState,
  subscribeLiveState: (_id: string, onState: (state: any) => Promise<void>) => {
    let live = true;
    const feed = async () => {
      while (live) {
        const state = await api.nextLiveState();
        if (!live) return;
        try { await onState(state); } catch { /* reconnect from fresh authoritative state */ }
      }
    };
    void feed();
    return () => { live = false; };
  },
  fetchTargets: api.fetchTargets,
  fetchStartupTargets: api.fetchStartupTargets,
  fetchTargetsForPath: api.fetchTargetsForPath,
  fetchAnnotations: api.fetchAnnotations,
  fetchFile: api.fetchFile,
  fetchRevisionLocator: api.fetchRevisionLocator,
  fetchRevisions: api.fetchRevisions,
  fetchRevisionComparison: api.fetchRevisionComparison,
  fetchSourceComparison: api.fetchSourceComparison,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchReviewReadiness: api.fetchReviewReadiness,
  armReviewLanding: api.armReviewLanding,
  cancelReviewLanding: api.cancelReviewLanding,
  fetchReviewOutcome: api.fetchReviewOutcome,
  fetchReviewSurfaces: api.fetchReviewSurfaces,
  fetchSourceState: api.fetchSourceState,
  subscribeSourceState: (_id: string, onState: (state: any) => void, retryMs = 2000) => {
    let live = true;
    let timer = 0;
    const feed = async () => {
      const state = await api.fetchSourceState();
      if (!live) return;
      onState(state);
      timer = window.setTimeout(() => void feed(), retryMs);
    };
    void feed();
    return () => { live = false; window.clearTimeout(timer); };
  },
  fetchChangeProposals: api.fetchChangeProposals,
  fetchProposalSelection: api.fetchProposalSelection,
  fetchReviewPreparation: api.fetchReviewPreparation,
  fetchReviewPatch: api.fetchReviewPatch,
  fetchFinishReview: vi.fn(),
  fetchRelatedSource: vi.fn(),
  fetchHistoricalRelatedSource: vi.fn(),
  postMark: api.postMark,
  postBulkReviewed: vi.fn(),
  undoBulkReviewed: vi.fn(),
  postRefresh: vi.fn(),
  postReviewOutcome: vi.fn(),
  postAnnotation: vi.fn(),
  patchAnnotation: vi.fn(),
  finishReview: vi.fn(),
  exportFeedback: vi.fn(),
  deliverFeedback: api.deliverFeedback,
  createChangeProposal: api.createChangeProposal,
  applyChangeProposal: api.applyChangeProposal,
  uploadReviewEvidence: vi.fn(),
  linkReviewEvidence: vi.fn(),
  runReviewSurface: vi.fn(),
  fetchFixLines: api.fetchFixLines,
}));

const target = {
  target_id: "target-1",
  unit_key: "sym:app:run",
  kind: "symbol",
  path: "src/app.py",
  label: "src/app.py::run",
  symbol: "run",
  start_line: 1,
  end_line: 2,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 1, end_line: 2, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 1,
  attention_level: "normal",
  reasons: ["implementation change"],
  additions: 1,
  deletions: 0,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

const overview = {
  session: { id: "rev-history", title: "Historical fixture", subject_type: "local_change", range_mode: "working_tree", source_ref: "", status: "open", actor_type: "agent", reviewer_id: "local", repo_root: "/repo", updated_at: "2026-09-15T20:00:00Z" },
  revision: { id: "11111111111171118111111111111111", ref: "rr/11111111111171118111111111111111", revision_number: 0, range_mode: "working_tree", base_sha: "base", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T19:00:00Z" },
  revision_count: 2,
  historical: true,
  latest_revision_number: 1,
  latest_revision: { id: "22222222222272228222222222222222", ref: "rr/22222222222272228222222222222222", revision_number: 1, range_mode: "working_tree", base_sha: "base", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T20:00:00Z" },
  historical_judgment_complete: false,
  packet_available: true,
  stats: {},
  title: "Historical fixture",
  provenance: { status: "unknown", host: "", model: "", session_id: "", task: "", certainty: "none", match_confidence: 0, match_reason: "", commands_run: [], subagents: [], reads_recorded: false, inspected: null, uninspected_impacted: [] },
  evidence: [],
  degraded: [],
  groups: [],
  group_counts: { needs_attention: 0, changed: 0, unreviewed: 1, reviewed: 0, mechanical: 0 },
  unit_count: 1,
  target_count: 1,
  progress: { target_count: 1, reviewed: 0, unreviewed: 1, needs_changes: 0, changed_since_review: 0, unknown: 0 },
  outline: [{ path: "src/app.py", reviewed: 0, target_count: 1, state: "unreviewed", attention_level: "normal", reasons: ["implementation change"], additions: 1, deletions: 0 }],
  mark_count: 0,
  discarded: [],
} as any;

describe("ReviewReader historical route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
    api.fetchHistoricalOverview.mockResolvedValue(overview);
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    api.fetchHistoricalTargets.mockResolvedValue({ revision_id: "11111111111171118111111111111111", historical: true, order: "recommended", targets: [target], progress: overview.progress, outline: overview.outline });
    api.fetchStartupTargets.mockImplementation((id: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
    api.fetchTargetsForPath.mockImplementation((id: string, _path: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
    api.fetchHistoricalAnnotations.mockResolvedValue({ revision_id: "11111111111171118111111111111111", historical: true, annotations: [], counts: {} });
    api.fetchHistoricalFile.mockResolvedValue({ path: "src/app.py", status: "modified", language: "py", additions: 1, deletions: 0, patch: "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+historical\n", renderable: true, refusal: "", detail: "", symbols: [], provenance: overview.provenance, evidence: [], degraded: [] });
    api.fetchHistoricalReviewJournal.mockResolvedValue({ available: false, reason: "", review_id: overview.session.id, revision_id: overview.revision.id, revision_number: overview.revision.revision_number, session_id: "", host: "", entries: [] });
    api.fetchHistoricalReviewDevelopment.mockResolvedValue(null);
    api.fetchReviewDevelopment.mockResolvedValue(null);
    api.fetchReviewJournal.mockResolvedValue({ available: false, reason: "", review_id: overview.session.id, revision_id: overview.latest_revision.id, revision_number: overview.latest_revision.revision_number, session_id: "", host: "", entries: [] });
    api.fetchFixLines.mockResolvedValue({ fix_lines: [], fix_of: null });
    api.nextLiveState.mockImplementation(() => new Promise(() => {}));
    api.fetchReviewPreparation.mockResolvedValue({ state: "ready", detail: "" });
    api.fetchReviewEvidence.mockResolvedValue({ evidence: [] });
    api.fetchReviewSurfaces.mockResolvedValue({ surfaces: [] });
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { ...overview.revision, is_last_reviewed: true },
        { ...overview.latest_revision, is_latest: true },
      ],
      latest_revision_number: overview.latest_revision_number,
      last_reviewed_revision_number: overview.revision.revision_number,
      last_commented_revision_number: overview.revision.revision_number,
      default_base_revision_number: overview.revision.revision_number,
      default_target_revision_number: overview.latest_revision_number,
    });
    api.fetchRevisionComparison.mockImplementation(async (_id: string, base?: number, targetRevision?: number) => ({
      review_id: overview.session.id,
      reviewer_id: "local",
      from_revision: base === overview.latest_revision_number ? overview.latest_revision : overview.revision,
      to_revision: targetRevision === overview.latest_revision_number ? overview.latest_revision : overview.revision,
      suggested_from_revision_number: overview.revision.revision_number,
      revision_state: {
        latest_revision_number: overview.latest_revision_number,
        last_reviewed_revision_number: overview.revision.revision_number,
        last_commented_revision_number: overview.revision.revision_number,
        default_base_revision_number: overview.revision.revision_number,
        default_target_revision_number: overview.latest_revision_number,
      },
      annotations: [],
      annotation_groups: { originating: [], inherited: [], addressed_by_target: [] },
      summary: { added: 0, changed: 0, removed: 0, unchanged: 0 },
      files: [],
      code_available: true,
      code_unavailable_reason: "",
      code_summary: { files: 0, added_lines: 0, removed_lines: 0 },
      code_files: [],
      units: [],
    }));
  });

  it("loads only revision-specific reads and keeps human judgment immutable", async () => {
    render(
      <MemoryRouter initialEntries={["/reviews/rev-history/revisions/0#t=t&r=rev-history"]}>
        <Routes>
          <Route path="/reviews/:reviewId/revisions/:revisionNumber" element={<ReviewReader />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("1 historical target")).toBeTruthy();
    const historicalBanner = screen.getByTestId("historical-revision-banner");
    expect(historicalBanner).toBeTruthy();
    expect(within(historicalBanner).getByText("Historical")).toBeTruthy();
    expect(within(historicalBanner).getByText("Snapshot 0")).toBeTruthy();
    expect(within(historicalBanner).getByText("latest Snapshot 1")).toBeTruthy();
    expect(within(historicalBanner).getByText("read-only")).toBeTruthy();
    expect(screen.queryByText(/immutable, read-only snapshot/i)).toBeNull();
    expect(screen.queryByText(/Review state is replayed from history/i)).toBeNull();
    expect(screen.getByRole("link", { name: "Compare with latest" }).getAttribute("href")).toBe("/r/rev-history?base=0&target=1");
    expect(within(historicalBanner).getByRole("button", { name: "Back to latest · Snapshot 1" })).toBeTruthy();
    expect(api.fetchHistoricalOverview).toHaveBeenCalledWith("rev-history", 0);
    expect(api.fetchHistoricalFile).toHaveBeenCalledWith("rev-history", 0, "src/app.py");
    expect(api.fetchOverview).not.toHaveBeenCalled();
    expect(api.fetchFile).not.toHaveBeenCalled();

    await userEvent.keyboard("r");
    await waitFor(() => expect(api.postMark).not.toHaveBeenCalled());
  });

  it("treats /rr/<latest> as live latest state and resumes new-revision capture", async () => {
    const latest = {
      ...overview,
      session: { ...overview.session, id: "aaaaaaaaaaaaaaaa7aaa8aaaaaaaaaaa", ref: "r/aaaaaaaaaaaaaaaa7aaa8aaaaaaaaaaa" },
      revision: { ...overview.latest_revision },
      revision_count: 2,
      historical: true,
      latest_revision_number: 1,
      latest_revision: { ...overview.latest_revision },
    };
    api.fetchRevisionLocator.mockResolvedValue({ review: latest.session, revision: latest.revision });
    api.fetchHistoricalOverview.mockResolvedValue(latest);
    api.fetchHistoricalTargets.mockResolvedValue({ revision_id: latest.revision.id, historical: true, order: "recommended", targets: [target], progress: latest.progress, outline: latest.outline });
    api.fetchHistoricalAnnotations.mockResolvedValue({ revision_id: latest.revision.id, historical: true, annotations: [], counts: {} });
    api.fetchHistoricalFile.mockResolvedValue({ path: "src/app.py", status: "modified", language: "py", additions: 1, deletions: 0, patch: "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+latest\n", renderable: true, refusal: "", detail: "", symbols: [], provenance: latest.provenance, evidence: [], degraded: [] });
    api.fetchOverview.mockResolvedValue({ ...latest, historical: false });
    api.fetchTargets.mockResolvedValue({ revision_id: latest.revision.id, historical: false, order: "recommended", targets: [target], progress: latest.progress, outline: latest.outline });
    api.fetchAnnotations.mockResolvedValue({ annotations: [], counts: {} });
    api.fetchFile.mockResolvedValue({ path: "src/app.py", status: "modified", language: "py", additions: 1, deletions: 0, patch: "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+latest\n", renderable: true, refusal: "", detail: "", symbols: [], provenance: latest.provenance, evidence: [], degraded: [] });
    api.fetchReviewEvidence.mockResolvedValue({ evidence: [] });
    api.fetchReviewReadiness.mockResolvedValue({
      review_id: overview.session.id,
      snapshot_id: overview.latest_revision.id,
      snapshot_number: overview.latest_revision.revision_number,
      tree_fingerprint: "tree-latest",
      source_fingerprint: "source-latest",
      conditions: [],
      ready: false,
      armable: false,
      blocker_count: 0,
      local_blocker_count: 0,
      conditions_hash: "conditions-latest",
      work_items: [],
      landing: {
        supported: false,
        provider: "",
        state: "unsupported",
        armed: false,
        semantic_stale: false,
        revision_id: overview.latest_revision.id,
        tree_fingerprint: "tree-latest",
        conditions_hash: "conditions-latest",
        detail: "",
        url: "",
        mechanism: "",
      },
    });
    api.fetchReviewOutcome.mockResolvedValue({ current: null });
    api.fetchReviewSurfaces.mockResolvedValue({ surfaces: [] });
    api.fetchChangeProposals.mockResolvedValue({ revision_id: latest.revision.id, source_mutation_supported: false, proposals: [] });
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, settled: true, fingerprint: "worktree-ahead", path_count: 3, paths: ["a", "b", "c"], reason: "" });
    api.fetchReviewJournal.mockResolvedValue({ available: true, reason: "", review_id: latest.session.id, revision_id: latest.revision.id, revision_number: 2, session_id: "", host: "", entries: [] });

    render(
      <MemoryRouter initialEntries={[`/rr/${latest.revision.id}`]}>
        <Routes>
          <Route path="/rr/:revisionRef" element={<ReviewReader sourceProbeIntervalMs={1} />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledWith(latest.session.id));
    await waitFor(() => expect(screen.queryByTestId("historical-revision-banner")).toBeNull());
    await waitFor(() => expect(api.fetchSourceState.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Capture current source" }).length).toBeGreaterThanOrEqual(1);
  });

});
