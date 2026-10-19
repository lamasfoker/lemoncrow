import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const api = vi.hoisted(() => ({ fetchReviewAutomation: vi.fn(), runPostMergeAction: vi.fn() }));
vi.mock("./automationApi", () => ({
  fetchReviewAutomation: api.fetchReviewAutomation,
  runPostMergeAction: api.runPostMergeAction,
}));

import ReviewAutomationSection from "./ReviewAutomationSection";
import type { ReviewAutomationView } from "./automationApi";

function view(overrides: Partial<ReviewAutomationView> = {}): ReviewAutomationView {
  return {
    review_id: "rev-1",
    repository: "repo-1",
    policy: {
      rules: [
        {
          id: "auto-approve",
          description: "",
          trigger: "readiness_ready",
          action: "landing.auto_approve",
          action_label: "Approve on the provider",
          state_changing: true,
          reserved: true,
          permission: "review:judge",
          enabled: true,
          enabled_by: "ops-admin@acme",
          layer: "organization",
          source: "operator_review_policy",
          acts_as: { subject: "automation-reviewer", role: "reviewer" },
          repositories: [],
          conditions: [],
          params: {},
        },
      ],
      errors: [],
      source: "operator_review_policy",
      edit_source: "operator Review policy (defaults.automation); never repository text",
    },
    facts: {
      branch_protection: {
        requirements: "satisfied",
        required_reviews: "unknown",
        changes_requested: false,
        source: "provider_truth",
        detail: "2 provider requirement(s) read",
        rows: [],
      },
    },
    evaluations: [
      {
        rule_id: "auto-approve",
        action: "landing.auto_approve",
        trigger: "readiness_ready",
        would_fire: false,
        reason: "A guard does not hold: provider required reviews satisfied.",
        guards: [
          { name: "provider branch protection satisfied", kind: "guard", passed: true, detail: "Every provider landing requirement is satisfied.", fact: "branch_protection.requirements", op: "eq", expected: "satisfied", actual: "satisfied" },
          { name: "provider required reviews satisfied", kind: "guard", passed: false, detail: "Provider required reviews are unknown.", fact: "branch_protection.required_reviews", op: "eq", expected: "satisfied", actual: "unknown" },
        ],
        conditions: [],
        branch_protection: {
          requirements: "satisfied",
          required_reviews: "unknown",
          changes_requested: false,
          source: "provider_truth",
          detail: "",
          rows: [],
        },
        branch_protection_enforced: true,
      },
    ],
    decisions: [],
    merge: null,
    post_merge: {
      available: false,
      reason: "Post-merge actions run only after the connected provider reports this Change merged.",
      in_readiness: false,
      kinds: [
        { kind: "notification", action: "post_merge.notification", label: "Notify after merge", permission: "review:manage", state_changing: false, latest: null, runnable: false },
      ],
      records: [],
    },
    readiness_digest: "d",
    ...overrides,
  };
}

describe("ReviewAutomationSection", () => {
  beforeEach(() => {
    api.fetchReviewAutomation.mockReset();
    api.runPostMergeAction.mockReset();
  });

  it("shows each rule's source, acting identity and why it would not fire, with branch protection", async () => {
    api.fetchReviewAutomation.mockResolvedValue(view());
    render(<ReviewAutomationSection reviewId="rev-1" />);
    const rules = await screen.findByRole("list", { name: "Automation rules" });
    expect(rules).toHaveTextContent("Approve on the provider");
    expect(rules).toHaveTextContent("evaluated only");
    expect(rules).toHaveTextContent("would not fire");
    expect(rules).toHaveTextContent("Enabled by ops-admin@acme · acts as automation-reviewer (reviewer) · needs review:judge");
    const checks = screen.getByRole("list", { name: "Checks for auto-approve" });
    expect(checks).toHaveTextContent("provider required reviews satisfied");
    expect(screen.getByLabelText("does not hold")).toBeTruthy();
    expect(screen.getByText(/never repository text/)).toBeTruthy();
    expect(screen.getByLabelText("Provider branch protection")).toHaveTextContent(
      "required reviews unknown (never treated as satisfied)",
    );
    expect(screen.getByText(/No rule can bypass branch protection or required reviews/)).toBeTruthy();
    // Before a provider-observed merge, nothing can run.
    expect(screen.getByText(/run only after the connected provider reports this Change merged/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run Notify after merge" })).toBeNull();
  });

  it("runs a post-merge action on an explicit click after the merge and shows the rule-run record", async () => {
    const merged = view({
      merge: { source: "provider_subject", provider: "github", merged_head: "abc", reviewed_head_merged: true, detail: "" },
      post_merge: {
        available: true,
        reason: "The provider reported this Change merged.",
        in_readiness: false,
        kinds: [
          { kind: "notification", action: "post_merge.notification", label: "Notify after merge", permission: "review:manage", state_changing: false, latest: null, runnable: true },
          {
            kind: "changelog_entry",
            action: "post_merge.changelog_entry",
            label: "Draft changelog entry",
            permission: "review:manage",
            state_changing: false,
            runnable: true,
            latest: {
              id: "pma-1",
              kind: "changelog_entry",
              label: "Draft changelog entry",
              status: "completed",
              initiated_by: "rule",
              actor_id: "automation-developer",
              rule_id: "changelog-on-merge",
              acts_as: { subject: "automation-developer", role: "developer" },
              permission: "review:manage",
              detail: "",
              output: {},
              readiness_unchanged: true,
              attempts: 1,
            },
          },
        ],
        records: [],
      },
    });
    api.fetchReviewAutomation.mockResolvedValue(merged);
    api.runPostMergeAction.mockResolvedValue({ record: {}, ran: true });
    render(<ReviewAutomationSection reviewId="rev-1" />);
    const actions = await screen.findByRole("list", { name: "Post-merge actions" });
    expect(actions).toHaveTextContent("By rule changelog-on-merge · readiness record unchanged");
    expect(screen.queryByRole("button", { name: "Run Draft changelog entry" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Run Notify after merge" }));
    expect(api.runPostMergeAction).toHaveBeenCalledWith("rev-1", "notification");
    await waitFor(() => expect(api.fetchReviewAutomation).toHaveBeenCalledTimes(2));
  });

  it("offers no run in a read-only Review and reports an unavailable automation view", async () => {
    api.fetchReviewAutomation.mockResolvedValueOnce(view({
      post_merge: { ...view().post_merge, available: true, kinds: [{ ...view().post_merge.kinds[0], runnable: true }] },
    }));
    const { unmount } = render(<ReviewAutomationSection reviewId="rev-1" readOnly />);
    await screen.findByRole("list", { name: "Post-merge actions" });
    expect(screen.queryByRole("button", { name: "Run Notify after merge" })).toBeNull();
    unmount();
    api.fetchReviewAutomation.mockRejectedValueOnce(new Error("503"));
    render(<ReviewAutomationSection reviewId="rev-1" />);
    expect(await screen.findByText("Automation is unavailable: 503")).toBeTruthy();
  });
});
