import Home, { reviewPlace } from "./Home";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api } from "../api";
import * as reviewApi from "../review/reviewApi";
import {
  LOCAL_RUNTIME_CAPABILITIES,
  ProductCapabilitiesProvider,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  RUNTIME_ONLY_PRODUCT_CAPABILITIES,
  RuntimeCapabilitiesProvider,
} from "../lib/RuntimeCapabilities";
import type { ReviewDirectoryRow } from "../review/types";

const row = (overrides: Partial<ReviewDirectoryRow>): ReviewDirectoryRow => ({
  id: "rev-1",
  ref: "r/abc",
  review_path: "/r/abc",
  repo_id: "repo_6bab3bd1e08710426daa8d09afdef3c9",
  title: "working tree",
  source_ref: "working_tree",
  status: "open",
  revision_number: 0,
  revision_id: "rrv-1",
  updated_at: "",
  created_at: "",
  ...overrides,
});

afterEach(() => vi.restoreAllMocks());

describe("Home source availability", () => {
  it("keeps review content while marking failed run history unavailable", async () => {
    vi.spyOn(api, "insightsWindow").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(api, "traces").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(api, "savingsSummary").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(reviewApi, "fetchReviewDirectory").mockResolvedValue({
      reviews: [row({ title: "A real review" })], next_cursor: "",
    } as Awaited<ReturnType<typeof reviewApi.fetchReviewDirectory>>);
    render(<MemoryRouter><RuntimeCapabilitiesProvider value={{ ...LOCAL_RUNTIME_CAPABILITIES, localIntegrations: false }}>
      <Home />
    </RuntimeCapabilitiesProvider></MemoryRouter>);
    expect(await screen.findByText("Coding-agent activity is unavailable.")).toBeInTheDocument();
    expect(screen.getAllByText("A real review")).toHaveLength(2);
    expect(screen.queryByText("No active runs.")).not.toBeInTheDocument();
    expect(screen.queryByText("No failed runs.")).not.toBeInTheDocument();
    expect(screen.queryByText("No recent runs.")).not.toBeInTheDocument();
    expect(screen.getByText("1 known items")).toBeInTheDocument();
  });
});

describe("Home product isolation", () => {
  it("does not call Runtime APIs in a Review-only installation", async () => {
    const insights = vi.spyOn(api, "insightsWindow");
    const traces = vi.spyOn(api, "traces");
    const savings = vi.spyOn(api, "savingsSummary");
    const reviews = vi.spyOn(reviewApi, "fetchReviewDirectory").mockResolvedValue({
      reviews: [], status: "all", query: "", next_cursor: "",
    });

    render(
      <MemoryRouter>
        <ProductCapabilitiesProvider value={REVIEW_ONLY_PRODUCT_CAPABILITIES}>
          <Home />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(reviews).toHaveBeenCalledTimes(1));
    // The bounded Inbox view mode, never the legacy list that opens every Review.
    expect(reviews.mock.calls[0][0].view).toBe("all");
    expect(insights).not.toHaveBeenCalled();
    expect(traces).not.toHaveBeenCalled();
    expect(savings).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Savings summary")).not.toBeInTheDocument();
    expect(screen.queryByText("Active now")).not.toBeInTheDocument();
  });

  it("does not call Review APIs in a Runtime-only installation", async () => {
    vi.spyOn(api, "insightsWindow").mockResolvedValue({} as Awaited<ReturnType<typeof api.insightsWindow>>);
    vi.spyOn(api, "traces").mockResolvedValue({
      items: [],
      metrics: {
        stats: { total: 0, success: 0, failed: 0, partial: 0 },
        hosts: [],
        domains: [],
        workspaces: [],
      },
    });
    const savings = vi.spyOn(api, "savingsSummary").mockResolvedValue({
      window_days: 7,
      total_naive_tokens: 0,
      total_actual_tokens: 0,
      reduction_pct: 0,
      per_lever: {},
      by_day: [],
    } as Awaited<ReturnType<typeof api.savingsSummary>>);
    const reviews = vi.spyOn(reviewApi, "fetchReviewDirectory");

    render(
      <MemoryRouter>
        <ProductCapabilitiesProvider value={RUNTIME_ONLY_PRODUCT_CAPABILITIES}>
          <Home />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(savings).toHaveBeenCalledTimes(1));
    expect(reviews).not.toHaveBeenCalled();
    expect(screen.queryByText("Recent reviews")).not.toBeInTheDocument();
  });
});

describe("Home review place", () => {
  it("names the project, a distinct worktree and the branch instead of the opaque repository id", () => {
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "agentic-review",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · agentic-review · main");
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "lemoncrow-dev",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · main");
    expect(reviewPlace(row({ directory: "demo" }))).toBe("demo");
  });

  it("falls back to the repository id only when nothing human is known", () => {
    expect(reviewPlace(row({}))).toBe("repo_6bab3bd1e08710426daa8d09afdef3c9");
  });
});
