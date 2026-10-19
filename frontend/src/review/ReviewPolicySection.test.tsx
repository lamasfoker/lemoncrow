import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewPolicySection from "./ReviewPolicySection";
import type { ReviewOverview, ReviewRuleAdoptionProposal } from "./types";

const adoptRuleSuggestion = vi.fn();
const dismissRuleSuggestion = vi.fn();

vi.mock("./reviewApi", () => ({
  adoptRuleSuggestion: (...args: unknown[]) => adoptRuleSuggestion(...args),
  dismissRuleSuggestion: (...args: unknown[]) => dismissRuleSuggestion(...args),
}));

const PROPOSAL: ReviewRuleAdoptionProposal = {
  suggestion_id: "rsg-1",
  state: "proposed",
  effective: false,
  stable_key: "learned-1",
  instruction: "Validate tenant ownership.",
  paths: ["src/api/users.py"],
  target_path: ".lemoncrow/review.yaml",
  change: "changed",
  base_sha256: "abc",
  patch: "diff --git a/.lemoncrow/review.yaml b/.lemoncrow/review.yaml\n+  - stable_key: \"learned-1\"\n",
  proposed_content: "rules:\n",
  supporting_feedback_refs: ["annotation:a1"],
  how_to_land: "Commit this change to .lemoncrow/review.yaml on the repository's trusted base.",
};

function overview(): ReviewOverview {
  return {
    session: { id: "review-1" },
    revision: { id: "rev-1" },
    policy: {
      authority: { base_state: "present", rules: [], ai_content_access: [], rule_documents: [], errors: [] },
      future_changes: [],
      suggestions: [
        {
          id: "rsg-1",
          repository_scope: "repo",
          candidate_statement: "Validate tenant ownership.",
          paths: ["src/api/users.py"],
          supporting_feedback_refs: ["annotation:a1", "annotation:a2"],
          supporting_count: 2,
          state: "suggested",
          active: false,
          created_at: "",
          updated_at: "",
        },
      ],
    },
  } as unknown as ReviewOverview;
}

describe("ReviewPolicySection rule adoption", () => {
  beforeEach(() => {
    adoptRuleSuggestion.mockReset();
    dismissRuleSuggestion.mockReset();
  });

  it("shows the proposed patch as not active and never marks the suggestion adopted", async () => {
    adoptRuleSuggestion.mockResolvedValue({ proposal: PROPOSAL });
    render(<ReviewPolicySection overview={overview()} />);
    await userEvent.click(screen.getByText(/Rule suggestions/));
    await userEvent.click(screen.getByRole("button", { name: "Propose as rule" }));

    await waitFor(() => expect(screen.getByTestId("rule-adoption-proposal")).toBeTruthy());
    expect(adoptRuleSuggestion).toHaveBeenCalledWith("review-1", "rsg-1", "rev-1");
    expect(screen.getByText(/not active until it lands on the trusted base/)).toBeTruthy();
    expect(screen.getByText(/stable_key: "learned-1"/)).toBeTruthy();
    // Still a suggestion: nothing in the UI claims the rule is in force.
    expect(screen.getByText("Suggestion only · not active")).toBeTruthy();
    expect(dismissRuleSuggestion).not.toHaveBeenCalled();
  });

  it("names why a rule cannot be proposed", async () => {
    adoptRuleSuggestion.mockRejectedValue(new Error("the trusted base cannot be read right now"));
    render(<ReviewPolicySection overview={overview()} />);
    await userEvent.click(screen.getByText(/Rule suggestions/));
    await userEvent.click(screen.getByRole("button", { name: "Propose as rule" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("the trusted base cannot be read"));
    expect(screen.queryByTestId("rule-adoption-proposal")).toBeNull();
  });
});
