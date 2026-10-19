import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ReviewTriageLine, { triageOriginLabel } from "./ReviewTriageLine";
import type { ReviewTriage } from "./types";

const TRUSTED = { layer: "trusted_base_repository", source: "base Snapshot .lemoncrow/review.yaml", commit: "c0ffeec0ffeec0ffee" };
const EVIDENCE = { layer: "evidence", source: "current Snapshot evidence", commit: "" };
const BUILT_IN = { layer: "built_in_default", source: "built_in_default", commit: "" };

function triage(options: Partial<ReviewTriage> = {}): ReviewTriage {
  return {
    version: 1,
    review_id: "r1",
    revision_id: "rev-1",
    priority: "high",
    computed_priority: "high",
    criticality: "critical",
    severity_summary: { p0: 0, p1: 1, p2: 0, suggestion: 0, unrated: 0, stale: 1, below_threshold: 2, highest: "p1" },
    finding_threshold: { value: "high", label: "High+", origin: TRUSTED },
    evidence_state: "missing",
    review_state: "not_reviewed",
    evidence_label: "Not reviewed · current evidence missing",
    release_urgency: "unknown",
    dependency_changes: 0,
    required_failures: 0,
    override: null,
    explanation: [
      { field: "priority", value: "high", reason: "highest signal: 1 unresolved P1 High finding(s) with current evidence", origin: EVIDENCE },
      { field: "criticality", value: "critical", reason: "src/auth/login.py matched src/auth/** (strictest rule wins)", origin: TRUSTED },
      { field: "release_urgency", value: "unknown", reason: "no linked issue or release source exists for this Review; urgency is never inferred", origin: BUILT_IN },
    ],
    hidden_finding_ids: ["a", "b"],
    policy_state: "final",
    policy_errors: [],
    computed: true,
    ...options,
  };
}

describe("ReviewTriageLine", () => {
  it("keeps priority, criticality, severity and missing evidence separate in one collapsed line", () => {
    render(<ReviewTriageLine triage={triage()} />);
    const line = screen.getByTestId("brief-review-priority");
    expect(line.tagName).toBe("DETAILS");
    expect(line).not.toHaveAttribute("open");
    const summary = within(line).getByText(/^Priority High/);
    expect(summary.textContent).toBe("Priority High · Critical criticality · 1 P1 · Not reviewed · current evidence missing");
  });

  it("explains each value with the layer and file or commit that set it", () => {
    render(<ReviewTriageLine triage={triage()} />);
    const rows = screen.getAllByTestId("triage-explanation").map((row) => row.textContent);
    expect(rows[0]).toContain("Priority · High — highest signal");
    expect(rows[0]).toContain("(Snapshot evidence)");
    expect(rows[1]).toContain("Criticality · Critical — src/auth/login.py matched src/auth/**");
    expect(rows[1]).toContain("(trusted base · base Snapshot .lemoncrow/review.yaml@c0ffeec0ffee)");
    expect(rows[2]).toContain("Release urgency · unknown");
    expect(screen.getByText(/2 findings below the High\+ threshold are not surfaced; still counted for priority\./)).toBeInTheDocument();
  });

  it("names an audited override and its actor without hiding the computed priority", () => {
    const value = triage({
      priority: "low",
      override: {
        level: "low",
        reason: "cosmetic",
        scope: "review",
        actor_id: "alice",
        actor_type: "human",
        revision_id: "rev-1",
        created_at: "2026-10-03T00:00:00Z",
        event_id: "rae-1",
      },
      explanation: [
        {
          field: "override",
          value: "low",
          reason: "review-scoped override (computed high): cosmetic",
          origin: { layer: "override", source: "human:alice at 2026-10-03T00:00:00Z", commit: "" },
        },
      ],
    });
    render(<ReviewTriageLine triage={value} />);
    expect(screen.getByTestId("brief-review-priority").querySelector("summary")?.textContent).toContain("Priority Low");
    expect(screen.getByText(/overridden/)).toBeInTheDocument();
    expect(screen.getByTestId("triage-explanation").textContent).toContain("(computed high): cosmetic (override · human:alice at 2026-10-03T00:00:00Z)");
  });

  it("renders nothing without a projection and labels origins compactly", () => {
    const { container } = render(<ReviewTriageLine triage={undefined} />);
    expect(container).toBeEmptyDOMElement();
    expect(triageOriginLabel(BUILT_IN)).toBe("built-in default");
    expect(triageOriginLabel({ layer: "organization", source: "operator_review_policy", commit: "" })).toBe(
      "organization policy · operator_review_policy",
    );
  });
});
