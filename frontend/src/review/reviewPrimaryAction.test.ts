import { describe, expect, it } from "vitest";

import type { ReviewGuideTarget } from "./readerModel";
import {
  deriveReviewPrimaryAction,
  finishAvailableInMenu,
  primaryActionDestination,
  type ReviewPrimaryActionKind,
  type ReviewPrimaryDestination,
} from "./reviewPrimaryAction";
import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { FeedbackDeliveryCapability, FeedbackStatus, ReviewProgress } from "./types";

const progress: ReviewProgress = {
  target_count: 4,
  reviewed: 4,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
};
const feedback: FeedbackStatus = { open_total: 0, unpublished: 0, published: 0, in_flight: 0, addressed: 0 };
const readiness: ReviewReadinessModel = {
  ready: true,
  label: "Ready for verdict",
  detail: "Ready",
  blockers: [],
  evidenceFacts: [],
  targetCount: 4,
  reviewedTargets: 4,
  changedSinceReview: 0,
  needsChanges: 0,
  targetPending: 0,
  openComments: 0,
  failedChecks: 0,
  unresolvedChecks: 0,
  outcome: null,
};
const claude: FeedbackDeliveryCapability = {
  supported: true,
  host: "claude",
  session_id: "s-1",
  target_ref: "claude:s-1",
  label: "Claude",
  reason: "",
};
const noAgent: FeedbackDeliveryCapability = {
  supported: false,
  host: "",
  session_id: "",
  target_ref: "",
  label: "",
  reason: "No exact author session",
};

type Input = Parameters<typeof deriveReviewPrimaryAction>[0];

function input(overrides: Partial<Input> = {}): Input {
  return {
    historical: false,
    sessionStatus: "open",
    revisionAdvanced: false,
    sourceChanged: false,
    commentsState: "ready",
    feedbackStatus: feedback,
    progress,
    readiness,
    nextTarget: null,
    ...overrides,
  };
}

function action(overrides: Partial<Input> = {}) {
  return deriveReviewPrimaryAction(input(overrides));
}

/** The queue head as `nextReviewGuideTarget` returns it. */
function head(targetId: string, reasonCode: string, reason: string): ReviewGuideTarget {
  return {
    targetId,
    path: "src/a.py",
    label: "authorize",
    reason,
    reasonCode,
    attention: ["request_change_open", "changed_since_judgment", "decision_scope_changed"].includes(reasonCode)
      ? "review_first"
      : "understand",
  };
}

interface Expected {
  kind: ReviewPrimaryActionKind;
  label: string;
  disabled: boolean;
  destination: ReviewPrimaryDestination;
  detail: string;
  /** "Finish review…" stays in the menu. */
  finishInMenu: boolean;
  opensReadiness: boolean;
  targetId?: string;
}

const github = (counts: Partial<NonNullable<FeedbackStatus["github"]>>, open = 2): FeedbackStatus => ({
  ...feedback,
  open_total: open,
  unpublished: counts.unpublished ?? 0,
  github: { unpublished: 0, published: 0, in_flight: 0, ...counts },
});

const FINISH_DETAIL = "Human judgments, discussions, and required verification have no current blockers.";

const MATRIX: Array<[string, Partial<Input>, Expected]> = [
  [
    "unreviewed work remains",
    { progress: { ...progress, reviewed: 2, unreviewed: 2 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Review next area", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-next" },
  ],
  [
    "unreviewed work remains before the queue has loaded",
    { progress: { ...progress, reviewed: 2, unreviewed: 2 } },
    { kind: "review_target", label: "Review next area", disabled: true, destination: "none", detail: "2 review areas still need human judgment.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes remain",
    { progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "review_requested_changes", label: "Review requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "requested changes head the queue ahead of changed and unreviewed work",
    {
      progress: { ...progress, reviewed: 0, changed_since_review: 1, needs_changes: 2, unreviewed: 1 },
      nextTarget: head("t-asked", "request_change_open", "Requested change is still open"),
    },
    { kind: "review_requested_changes", label: "Review 2 requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "a file-level requested change heads the queue without a needs-changes mark",
    {
      progress: { ...progress, reviewed: 2, unreviewed: 2 },
      nextTarget: head("t-file", "request_change_open", "Requested change is still open"),
    },
    { kind: "review_target", label: "Review next area", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-file" },
  ],
  [
    "content changed since review",
    { progress: { ...progress, reviewed: 3, changed_since_review: 1 }, nextTarget: head("t-changed", "changed_since_judgment", "Changed since your review") },
    { kind: "review_changed", label: "Re-review changed area", disabled: false, destination: "target", detail: "Changed since your review", finishInMenu: true, opensReadiness: false, targetId: "t-changed" },
  ],
  [
    "a reconsidered decision heads the queue ahead of changed areas",
    {
      progress: { ...progress, reviewed: 1, changed_since_review: 2, unreviewed: 1 },
      nextTarget: head("t-decision", "decision_scope_changed", "Decision: scope changed"),
    },
    { kind: "review_target", label: "Review next area", disabled: false, destination: "target", detail: "Decision: scope changed", finishInMenu: true, opensReadiness: false, targetId: "t-decision" },
  ],
  [
    "addressed comments need re-review",
    { feedbackStatus: { ...feedback, open_total: 2, addressed: 2 }, feedbackDelivery: claude },
    { kind: "rereview_comments", label: "Re-review 2", disabled: false, destination: "comments", detail: "The coding agent marked these comments addressed; only your re-review can resolve them.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback addressed by an author without an agent session",
    { feedbackStatus: { ...feedback, open_total: 1, addressed: 1 }, feedbackDelivery: noAgent },
    { kind: "rereview_comments", label: "Re-review 1", disabled: false, destination: "comments", detail: "The author marked these comments addressed; only your re-review can resolve them.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "verification fails",
    { readiness: { ...readiness, ready: false, failedChecks: 2, detail: "2 failed checks" } },
    { kind: "review_readiness", label: "Review 2 failed checks", disabled: false, destination: "readiness", detail: "Current verification explicitly reports failure.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "one check fails",
    { readiness: { ...readiness, ready: false, failedChecks: 1, detail: "1 failed check" } },
    { kind: "review_readiness", label: "Review failed check", disabled: false, destination: "readiness", detail: "Current verification explicitly reports failure.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "verification is unknown",
    { readiness: { ...readiness, ready: false, unresolvedChecks: 1, detail: "1 unresolved check" } },
    { kind: "review_readiness", label: "Review verification", disabled: false, destination: "readiness", detail: "Verification is not run, unknown, stale, or otherwise unresolved.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "open comments remain",
    { feedbackStatus: { ...feedback, open_total: 2 }, feedbackDelivery: claude },
    { kind: "review_comments", label: "Review comments", disabled: false, destination: "comments", detail: "Open human review discussions still need attention.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "unsent comments never take over the next area to review",
    { feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, unreviewed: 1 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Review next area", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-next" },
  ],
  [
    "unsent comments with everything reviewed go to the verdict",
    { feedbackStatus: { ...feedback, open_total: 1, unpublished: 1 }, feedbackDelivery: noAgent },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: false, opensReadiness: false },
  ],
  [
    "feedback delivery in flight",
    { feedbackStatus: { ...feedback, open_total: 1, in_flight: 1 }, feedbackDelivery: claude },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: true, destination: "none", detail: "Feedback is with Claude; the next Snapshot or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback with the agent, working",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: claude },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: true, destination: "none", detail: "Feedback is with Claude; the next Snapshot or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback copied for the author",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: { ...noAgent, supported: true, kind: "export", label: "author", target_ref: "clipboard:author" } },
    { kind: "waiting_agent", label: "Waiting on author", disabled: true, destination: "none", detail: "Feedback is with author; the next Snapshot or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes handed to the agent with nothing else to judge",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: true, destination: "none", detail: "Feedback is with Claude; the next Snapshot or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes not yet sent are still the reviewer's",
    { feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "review_requested_changes", label: "Review requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "all work reviewed",
    {},
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: false, opensReadiness: false },
  ],
  [
    "source drift alone is not a Review step",
    { sourceChanged: true },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: false, opensReadiness: false },
  ],
  [
    "GitHub PR review with comments not on the PR goes to the verdict",
    { feedbackStatus: github({ unpublished: 2 }), feedbackDelivery: noAgent },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: false, opensReadiness: false },
  ],
  [
    "GitHub PR review with a publish in doubt",
    { feedbackStatus: github({ unpublished: 1, in_flight: 1 }) },
    { kind: "delivery_pending", label: "Publish pending", disabled: true, destination: "none", detail: "A GitHub publish is in progress or its result is unknown; LemonCrow checks the pull request before posting again.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "GitHub PR review with everything published waits on the pull request",
    { feedbackStatus: github({ published: 2 }), feedbackDelivery: { ...noAgent, kind: "github", supported: true, label: "PR #7" } },
    { kind: "waiting_agent", label: "Waiting on PR #7", disabled: true, destination: "none", detail: "Feedback is with PR #7; the next Snapshot or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "hosted policy blocker",
    {
      readiness: {
        ...readiness,
        ready: false,
        label: "Not ready · 1",
        detail: "1 more approval required",
        blockers: [{ id: "policy:reviewers", label: "1 more approval required", detail: "Hosted policy", tone: "amber" }],
      },
    },
    { kind: "review_readiness", label: "Review blockers", disabled: false, destination: "readiness", detail: "1 more approval required", finishInMenu: true, opensReadiness: true },
  ],
  [
    "historical Snapshot",
    { historical: true, feedbackStatus: { ...feedback, open_total: 1, unpublished: 1 }, feedbackDelivery: claude },
    { kind: "go_latest", label: "Back to latest", disabled: false, destination: "latest_snapshot", detail: "Historical Snapshots are read-only. Return to the latest Snapshot.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished Review",
    { sessionStatus: "finished" },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished review with comments never sent reopens",
    { sessionStatus: "finished", feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished local review with a handoff in doubt",
    { sessionStatus: "finished", feedbackStatus: { ...feedback, open_total: 1, unpublished: 1, in_flight: 1 }, feedbackDelivery: claude },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished GitHub PR review whose comments are not on the PR reopens",
    { sessionStatus: "finished", feedbackStatus: github({ unpublished: 2 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished GitHub PR review with a publish in doubt",
    { sessionStatus: "finished", feedbackStatus: github({ unpublished: 1, in_flight: 1 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "archived Review",
    { sessionStatus: "archived", feedbackStatus: github({ unpublished: 2 }) },
    { kind: "restore", label: "Restore review", disabled: false, destination: "finish_sheet", detail: "Restore this archived review before making changes.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "degraded: content identity unavailable",
    { progress: { ...progress, reviewed: 3, unknown: 1 }, nextTarget: head("t-unknown", "fingerprint_unavailable", "Content identity unavailable; review individually") },
    { kind: "review_target", label: "Review next area", disabled: false, destination: "target", detail: "Content identity unavailable; review individually", finishInMenu: true, opensReadiness: false, targetId: "t-unknown" },
  ],
  [
    "degraded: landing conditions unavailable fail closed",
    {
      readiness: {
        ...readiness,
        ready: false,
        label: "Landing readiness unavailable",
        detail: "The server-authoritative landing conditions could not be loaded; readiness fails closed until they are available.",
      },
    },
    { kind: "review_readiness", label: "Review blockers", disabled: false, destination: "readiness", detail: "The server-authoritative landing conditions could not be loaded; readiness fails closed until they are available.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "degraded: discussions could not load",
    { commentsState: "failed" },
    { kind: "reload_state", label: "Retry review state", disabled: false, destination: "reload", detail: "Review discussions could not be loaded. Reload before deciding the review is complete.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "discussions still loading",
    { commentsState: "loading" },
    { kind: "checking_comments", label: "Checking comments…", disabled: true, destination: "none", detail: "Existing review discussions are still loading; readiness cannot assume there are none.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "newer Snapshot exists",
    { revisionAdvanced: true, progress: { ...progress, reviewed: 3, unreviewed: 1 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "reload_state", label: "Reload review", disabled: false, destination: "reload", detail: "A newer Snapshot exists and this one no longer accepts changes. Reload before judging the new Snapshot.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "newer Snapshot on a finished GitHub PR review never publishes from the old one",
    { sessionStatus: "finished", revisionAdvanced: true, feedbackStatus: github({ unpublished: 2 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
];

describe("deriveReviewPrimaryAction state matrix (plan §8.10)", () => {
  it.each(MATRIX)("%s", (_name, overrides, expected) => {
    const state = input(overrides);
    const result = deriveReviewPrimaryAction(state);
    const destination = primaryActionDestination(result);
    expect({
      kind: result.kind,
      label: result.label,
      disabled: result.disabled,
      destination,
      detail: result.detail,
      finishInMenu: finishAvailableInMenu({
        sessionStatus: state.sessionStatus,
        historical: state.historical,
        revisionAdvanced: state.revisionAdvanced,
        action: result,
      }),
      opensReadiness: destination === "readiness",
      ...(expected.targetId !== undefined ? { targetId: result.targetId } : {}),
    }).toEqual(expected);
  });

  it("gives every enabled action somewhere to go and every disabled one nowhere", () => {
    for (const [, overrides] of MATRIX) {
      const result = action(overrides);
      expect(primaryActionDestination(result) === "none").toBe(result.disabled);
    }
  });
});

describe("deriveReviewPrimaryAction", () => {
  it("counts a pull request Review's handoff on the PR, and an agent route's with the agent", () => {
    const github = { ...feedback, open_total: 2, unpublished: 2, github: { unpublished: 0, published: 2, in_flight: 0 } };
    expect(action({ feedbackStatus: github })).toMatchObject({ kind: "waiting_agent", label: "Waiting on author" });
    expect(action({ feedbackStatus: github, feedbackDelivery: { ...claude, kind: "github", label: "PR #3" } })).toMatchObject({ label: "Waiting on PR #3" });
    expect(action({ feedbackStatus: github, feedbackDelivery: claude })).toMatchObject({ kind: "finish" });
  });

  it("never offers finish while current target judgment remains", () => {
    expect(action({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", label: "Review next area", targetId: "t-1", disabled: false });
  });

  it("puts changed work ahead of ordinary pending review", () => {
    expect(action({
      progress: { ...progress, reviewed: 2, changed_since_review: 1, unreviewed: 1 },
      nextTarget: head("changed", "changed_since_judgment", "Changed since your review"),
    })).toMatchObject({ kind: "review_changed", label: "Re-review changed area", targetId: "changed" });
  });

  it("keeps remaining review ahead of a handoff that is out, and names the real destination once it waits", () => {
    const out = { ...feedback, open_total: 1, published: 1 };
    expect(action({
      feedbackStatus: out,
      feedbackDelivery: claude,
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", targetId: "t-1" });
    expect(action({
      feedbackStatus: out,
      feedbackDelivery: { ...claude, kind: "agent", label: "Claude a1b2" },
    })).toMatchObject({ kind: "waiting_agent", label: "Waiting on Claude a1b2" });
  });

  it("keeps source drift separate from review workflow while revision/loading failures remain recovery actions", () => {
    expect(action({ revisionAdvanced: true })).toMatchObject({ kind: "reload_state", label: "Reload review" });
    expect(action({
      sourceChanged: true,
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", label: "Review next area", targetId: "t-1" });
    expect(action({ commentsState: "failed" })).toMatchObject({ kind: "reload_state", label: "Retry review state" });
  });

  it("makes provider-native Merge when ready the lifecycle action once LemonCrow-owned work is clear", () => {
    const landing = {
      armable: true,
      landing: { supported: true, armed: false, provider: "github", state: "not_armed" },
    };
    expect(action({
      readiness: { ...readiness, ready: false, label: "1 landing condition unresolved", detail: "Required checks" },
      landing,
    })).toMatchObject({ kind: "review_readiness", label: "Merge when ready" });
    expect(primaryActionDestination(action({ landing }))).toBe("readiness");
    expect(action({
      landing: { ...landing, landing: { ...landing.landing, armed: true, state: "delegated" } },
    })).toMatchObject({ kind: "review_readiness", label: "Merge when ready enabled" });
  });

  it("treats a merged landing as done rather than offering to arm it again", () => {
    expect(action({
      landing: { armable: true, landing: { supported: true, armed: false, provider: "github", state: "merged" } },
    })).toMatchObject({ kind: "review_readiness", label: "Merged" });
  });

  it("lets server landing conditions outrank client verification placeholders", () => {
    const landing = {
      armable: false,
      landing: { supported: false, armed: false, provider: "", state: "" },
      conditions: [
        {
          id: "latest-snapshot-reviewed",
          label: "Latest revision reviewed",
          state: "pending" as const,
          detail: "1 review area still needs human judgment or re-review.",
          owner: "lemoncrow" as const,
          category: "human_review",
          url: "",
        },
        {
          id: "verification",
          label: "Verification requirements",
          state: "not_applicable" as const,
          detail: "No required verification result is configured or recorded for this Snapshot.",
          owner: "lemoncrow" as const,
          category: "verification",
          url: "",
        },
      ],
    };
    expect(action({
      readiness: {
        ...readiness,
        ready: false,
        unresolvedChecks: 5,
        detail: "5 unresolved checks",
      },
      landing,
    })).toMatchObject({
      kind: "review_readiness",
      label: "Review coverage",
      detail: "1 review area still needs human judgment or re-review.",
    });

    expect(action({
      readiness: {
        ...readiness,
        ready: false,
        unresolvedChecks: 5,
        detail: "5 unresolved checks",
      },
      landing: {
        ...landing,
        conditions: [
          { ...landing.conditions[0], state: "pass" as const },
          landing.conditions[1],
        ],
      },
    })).toMatchObject({
      kind: "review_readiness",
      label: "Review blockers",
    });
  });

  it("routes verification blockers to evidence rather than finish", () => {
    expect(action({
      readiness: { ...readiness, ready: false, failedChecks: 2, blockers: [{ id: "failed-checks", label: "2 failed checks", detail: "fail", tone: "rose" }] },
    })).toMatchObject({ kind: "review_readiness", label: "Review 2 failed checks" });
    expect(action({
      readiness: { ...readiness, ready: false, unresolvedChecks: 1, blockers: [{ id: "unresolved-checks", label: "1 unresolved check", detail: "unknown", tone: "amber" }] },
    })).toMatchObject({ kind: "review_readiness", label: "Review verification" });
  });

  it("only offers finish after durable state and readiness are clear", () => {
    expect(action()).toMatchObject({ kind: "finish", label: "Finish review", disabled: false });
  });
});
