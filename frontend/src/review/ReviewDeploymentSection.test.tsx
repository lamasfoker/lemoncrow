import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const api = vi.hoisted(() => ({ fetchReviewDeployments: vi.fn() }));
vi.mock("./deploymentsApi", () => ({ fetchReviewDeployments: api.fetchReviewDeployments }));

import ReviewDeploymentSection from "./ReviewDeploymentSection";
import type { DeploymentEnvironmentView, ReviewDeploymentsView } from "./deploymentsApi";

const MERGED = "d".repeat(40);
const CHILD = "e".repeat(40);
const CONNECTOR = { connector: "github_deployments", state: "configured" as const, locus: "provider_webhook", reason: "" };

function environment(overrides: Partial<DeploymentEnvironmentView>): DeploymentEnvironmentView {
  return {
    environment: "staging",
    key: "staging",
    state: "deployed",
    label: "deployed to staging",
    deciding_id: "501",
    commit: MERGED,
    relation: "exact",
    since: "2026-10-04T08:00:00.000000Z",
    open_until: "2026-10-05T08:00:00.000000Z",
    production: false,
    traffic_percent: null,
    url: "",
    provider: "github",
    entries_dropped: 0,
    ...overrides,
  };
}

function configured(overrides: Partial<ReviewDeploymentsView> = {}): ReviewDeploymentsView {
  return {
    configured: true,
    connector: CONNECTOR,
    merged: { provider: "github", sha: MERGED, merged_at: "2026-10-04T07:00:00.000000Z", ref: "main" },
    status_line: "Deployed to staging · production canary 10%",
    environments: [
      environment({}),
      environment({
        environment: "production",
        key: "production",
        label: "deployed to production canary 10%",
        deciding_id: "601",
        commit: CHILD,
        relation: "descendant",
        production: true,
        traffic_percent: 10,
      }),
    ],
    environments_dropped: 0,
    freshness_class: "external_observation",
    in_readiness: false,
    ...overrides,
  };
}

describe("ReviewDeploymentSection", () => {
  beforeEach(() => {
    api.fetchReviewDeployments.mockReset();
  });

  it("shows the status line and why each environment counts", async () => {
    api.fetchReviewDeployments.mockResolvedValue(configured());
    render(<ReviewDeploymentSection reviewId="rev-1" />);
    expect(await screen.findByTestId("deployment-status-line")).toHaveTextContent(
      "Deployed to staging · production canary 10%",
    );
    expect(api.fetchReviewDeployments).toHaveBeenCalledWith("rev-1");
    const rows = screen.getByRole("list", { name: "Deployment environments" });
    expect(rows).toHaveTextContent(`Deployed to staging · ${MERGED.slice(0, 12)} is the merged commit`);
    expect(rows).toHaveTextContent(`Deployed to production canary 10% · ${CHILD.slice(0, 12)} contains this change`);
    expect(screen.getByText(/never part of readiness/)).toBeInTheDocument();
    // A read-only line: no new control.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names a rollback and the commit that does not contain the change", async () => {
    api.fetchReviewDeployments.mockResolvedValue(
      configured({
        status_line: "Rolled back from production",
        environments: [
          environment({
            environment: "production",
            key: "production",
            state: "rolled_back",
            label: "rolled back from production",
            commit: "c".repeat(40),
            relation: "not_contained",
            production: true,
          }),
        ],
      }),
    );
    render(<ReviewDeploymentSection reviewId="rev-1" />);
    expect(await screen.findByTestId("deployment-status-line")).toHaveTextContent("Rolled back from production");
    expect(screen.getByText(/cccccccccccc does not contain this change/)).toBeInTheDocument();
  });

  it("says nothing was observed yet for a merged change without deployments", async () => {
    api.fetchReviewDeployments.mockResolvedValue(configured({ status_line: "", environments: [] }));
    render(<ReviewDeploymentSection reviewId="rev-1" />);
    expect(await screen.findByText(`No deployment of ${MERGED.slice(0, 12)} observed yet.`)).toBeInTheDocument();
    expect(screen.queryByTestId("deployment-status-line")).toBeNull();
  });

  it("renders nothing without a connector", async () => {
    api.fetchReviewDeployments.mockResolvedValue({
      configured: false,
      connector: { connector: "", state: "not_configured", locus: "", reason: "No deployment connector is configured." },
    });
    const { container } = render(<ReviewDeploymentSection reviewId="rev-1" />);
    await waitFor(() => expect(api.fetchReviewDeployments).toHaveBeenCalled());
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Deployments")).toBeNull();
  });

  it("renders nothing for an unconfigured projection even if it carried rows", async () => {
    api.fetchReviewDeployments.mockResolvedValue({ ...configured(), configured: false });
    const { container } = render(<ReviewDeploymentSection reviewId="rev-1" />);
    await waitFor(() => expect(api.fetchReviewDeployments).toHaveBeenCalled());
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing before the merge", async () => {
    api.fetchReviewDeployments.mockResolvedValue(configured({ merged: null, status_line: "", environments: [] }));
    const { container } = render(<ReviewDeploymentSection reviewId="rev-1" />);
    await waitFor(() => expect(api.fetchReviewDeployments).toHaveBeenCalled());
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it("reports an unavailable projection without blocking the Review", async () => {
    api.fetchReviewDeployments.mockRejectedValue(new Error("boom"));
    render(<ReviewDeploymentSection reviewId="rev-1" />);
    expect(await screen.findByText("Deployments are unavailable: boom")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
