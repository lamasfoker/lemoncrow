import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

async function settled() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

const api = vi.hoisted(() => ({ fetchReviewProduction: vi.fn() }));
vi.mock("./productionApi", () => ({ fetchReviewProduction: api.fetchReviewProduction }));

import ReviewProductionSection from "./ReviewProductionSection";
import type { ProductionObservationView, ProductionProblem, ReviewProductionView } from "./productionApi";

const observation: ProductionObservationView = {
  environment: "production",
  deployment_id: "601",
  provider: "github",
  merged_commit: "d".repeat(40),
  window: { start: "2026-10-04T08:00:00Z", end: "2026-10-05T08:00:00Z", baseline_start: "2026-10-03T08:00:00Z" },
  state: "observing",
  close_reason: "",
  counts: { contradicted: 1, confirmed: 1 },
  outcomes: [],
  predictions_dropped: 0,
  budget: 24,
  spent: 9,
  queries_planned: 9,
  queries_answered: 9,
};

const contradicted: ProductionProblem = {
  id: "outcome:contradicted:601:host",
  kind: "contradicted",
  title: "Production contradicted: removed: outbound_host:old.example.com",
  detail: "",
  revision_id: "rev-1",
  environment: "production",
  observed_at: "2026-10-04T09:00:00Z",
  advisory: false,
};

const incident: ProductionProblem = {
  ...contradicted,
  id: "outcome:incident:alert:a-1",
  kind: "incident",
  title: "Incident HighErrorRate may be related: touched route POST /v1/charges; Correlation, not a cause.",
  advisory: true,
};

function view(overrides: Partial<ReviewProductionView> = {}): ReviewProductionView {
  return {
    configured: true,
    observability: {
      configured: true,
      locus: "customer_worker",
      connectors: [{ id: "prometheus", kind: "prometheus" }],
      status_line: "Observing production until 2026-10-05 08:00 UTC: 1 contradicted, 1 confirmed",
      observations: [observation],
      incidents: [],
      incidents_total: 0,
    },
    problems: [contradicted, incident],
    links: [],
    rollback: { decision: { mode: "needs_human", reason: "no rule", rule_id: "" }, adapter: "", automatic: false, reason: "no rule" },
    freshness_class: "external_observation",
    in_readiness: false,
    ...overrides,
  };
}

describe("ReviewProductionSection", () => {
  beforeEach(() => {
    api.fetchReviewProduction.mockReset();
  });

  it("shows predicted vs observed, advisory incidents and that nothing runs without a person", async () => {
    api.fetchReviewProduction.mockResolvedValue(view());
    render(<ReviewProductionSection reviewId="rev-1" variant="status" />);
    expect(await screen.findByTestId("production-status-line")).toHaveTextContent("1 contradicted, 1 confirmed");
    expect(api.fetchReviewProduction).toHaveBeenCalledWith("rev-1");
    const problems = screen.getByRole("list", { name: "Production problems" });
    expect(problems).toHaveTextContent("Production contradicted: removed: outbound_host:old.example.com");
    expect(problems).toHaveTextContent("advisory, correlation is not a cause");
    expect(screen.getByText(/nothing runs without you/)).toBeInTheDocument();
    expect(screen.getByText(/never part of readiness/)).toBeInTheDocument();
    // Read-only lines: no new control.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names the Revert and Fix forward Changes linked both ways and an enabled rollback rule", async () => {
    api.fetchReviewProduction.mockResolvedValue(
      view({
        links: [
          { relation: "reverted_by", target_review_id: "rev-2", title: "Revert #42", created_at: "" },
          { relation: "fixed_forward_by", target_review_id: "rev-3", title: "", created_at: "" },
        ],
        rollback: {
          decision: { mode: "rule", reason: "enabled", rule_id: "roll-back" },
          adapter: "",
          automatic: false,
          reason: "no CD rollback adapter is configured",
        },
      }),
    );
    render(<ReviewProductionSection reviewId="rev-1" variant="status" />);
    const links = await screen.findByRole("list", { name: "Linked Changes" });
    expect(links).toHaveTextContent("Reverted by Revert #42");
    expect(links).toHaveTextContent("Fixed forward by rev-3");
    expect(screen.getByText(/Automatic rollback \(rule roll-back\): no CD rollback adapter is configured/)).toBeInTheDocument();
  });

  it("renders nothing without a connector or before anything was observed", async () => {
    // ``configured: false`` always wins, whatever else a response carries.
    api.fetchReviewProduction.mockResolvedValue({ ...view(), configured: false });
    const { container, unmount } = render(<ReviewProductionSection reviewId="rev-1" variant="status" />);
    await waitFor(() => expect(api.fetchReviewProduction).toHaveBeenCalled());
    await settled();
    expect(container).toBeEmptyDOMElement();
    unmount();

    api.fetchReviewProduction.mockResolvedValue(
      view({ problems: [], observability: { configured: true, status_line: "No production observation yet", observations: [] } }),
    );
    const quiet = render(<ReviewProductionSection reviewId="rev-1" variant="status" />);
    await waitFor(() => expect(api.fetchReviewProduction).toHaveBeenCalledTimes(2));
    await settled();
    expect(quiet.container).toBeEmptyDOMElement();
  });

  it("puts only open problems in the Brief and hides errors there", async () => {
    api.fetchReviewProduction.mockResolvedValue(view());
    const { unmount } = render(<ReviewProductionSection reviewId="rev-1" variant="brief" />);
    const brief = await screen.findByTestId("brief-production");
    expect(brief).toHaveTextContent("After merge");
    expect(brief).toHaveTextContent("Production contradicted");
    expect(brief).not.toHaveTextContent("Observing production");
    unmount();

    api.fetchReviewProduction.mockResolvedValue(view({ problems: [] }));
    const none = render(<ReviewProductionSection reviewId="rev-1" variant="brief" />);
    await waitFor(() => expect(api.fetchReviewProduction).toHaveBeenCalledTimes(2));
    await settled();
    expect(none.container).toBeEmptyDOMElement();
    none.unmount();

    api.fetchReviewProduction.mockRejectedValue(new Error("offline"));
    const failed = render(<ReviewProductionSection reviewId="rev-1" variant="brief" />);
    await waitFor(() => expect(api.fetchReviewProduction).toHaveBeenCalledTimes(3));
    await settled();
    expect(failed.container).toBeEmptyDOMElement();
  });
});
