import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fetchReviewEvidenceContent: vi.fn() }));
vi.mock("./reviewApi", () => api);

import BrowserRegressionResult from "./BrowserRegressionResult";
import type { ReviewVerificationCheck } from "./types";

function artifact(id: string, mimeType = "image/png") {
  return {
    evidence_id: id,
    digest: `${id}-digest-0123456789`,
    mime_type: mimeType,
    bytes: 1,
    observed_at: "2026-09-25T00:00:00+00:00",
    snapshot_id: "rr-1",
    snapshot_number: 1,
  };
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    evidence_id: "ev-run",
    record_digest: "record-digest-0123456789",
    snapshot_id: "rr-1",
    snapshot_number: 1,
    side: "new",
    status: "PASS",
    status_reasons: [],
    observed_at: "",
    source: "ci",
    provider: "playwright",
    run_ref: "",
    target_url: "",
    viewport: { width: 1280, height: 800 },
    environment: { browser: "chromium" },
    completed: true,
    navigation: {},
    assertions: [],
    console: [],
    runtime_errors: [],
    visual: { diff_ratio: null, threshold: null, threshold_source: "" },
    observations: [],
    artifacts: { screenshot: artifact("ev-after") },
    started_at: "",
    finished_at: "",
    ...overrides,
  };
}

function check(browserRun: ReturnType<typeof run>, baseline: ReturnType<typeof run> | null): ReviewVerificationCheck {
  return {
    name: "Checkout regression",
    kind: "browser",
    status: browserRun.status,
    browser: { scenario: "checkout", route: "/checkout", configured: true, applicability: "changed path", run: browserRun, baseline },
  } as unknown as ReviewVerificationCheck;
}

async function expand() {
  await userEvent.click(screen.getByText(/What the browser observed/));
}

describe("BrowserRegressionResult error paths", () => {
  beforeEach(() => {
    let next = 0;
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:evidence-${++next}`) });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  });

  afterEach(() => {
    cleanup();
    api.fetchReviewEvidenceContent.mockReset();
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it("shows the after screenshot alone and says so when no baseline was recorded", async () => {
    api.fetchReviewEvidenceContent.mockResolvedValue(new Blob(["png"]));
    render(<BrowserRegressionResult check={check(run(), null)} />);

    expect(api.fetchReviewEvidenceContent).not.toHaveBeenCalled();
    await expand();

    const image = await screen.findByAltText("Browser screenshot for Snapshot 1");
    expect(image.getAttribute("src")).toMatch(/^blob:evidence-/);
    expect(screen.getByText(/no baseline screenshot recorded/)).toBeTruthy();
    expect(screen.getByText("No frozen baseline run was recorded for this scenario and viewport.")).toBeTruthy();
    expect(screen.queryByTestId("image-overlay-compare")).toBeNull();
    expect(api.fetchReviewEvidenceContent.mock.calls.map(([id]) => id)).toEqual(["ev-after"]);
  });

  it("keeps a single-sided screenshot when the baseline run captured none", async () => {
    api.fetchReviewEvidenceContent.mockResolvedValue(new Blob(["png"]));
    const baseline = run({ evidence_id: "ev-base", side: "old", relation: "base", artifacts: {} });
    render(<BrowserRegressionResult check={check(run(), baseline)} />);
    await expand();

    expect(await screen.findByAltText("Browser screenshot for Snapshot 1")).toBeTruthy();
    expect(screen.getByText(/no baseline screenshot recorded/)).toBeTruthy();
    expect(screen.queryByTestId("image-overlay-compare")).toBeNull();
    expect(screen.getByText(/pre-change side of this Snapshot/)).toBeTruthy();
  });

  it("names failed evidence fetches instead of loading forever", async () => {
    api.fetchReviewEvidenceContent.mockRejectedValue(new Error("evidence bytes are unavailable"));
    const failing = run({
      status: "FAIL",
      artifacts: { screenshot: artifact("ev-after"), video: artifact("ev-video", "video/webm") },
    });
    const baseline = run({
      evidence_id: "ev-base",
      side: "old",
      relation: "base",
      artifacts: { screenshot: artifact("ev-before"), video: artifact("ev-video-before", "video/webm") },
    });
    render(<BrowserRegressionResult check={check(failing, baseline)} />);
    await expand();

    expect(await screen.findByText("Screenshots unavailable: evidence bytes are unavailable")).toBeTruthy();
    expect(await screen.findByText("Recordings unavailable: evidence bytes are unavailable")).toBeTruthy();
    expect(screen.queryByText("Loading frozen screenshots…")).toBeNull();
    expect(screen.queryByTestId("image-overlay-compare")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows a failed trace download next to the trace link", async () => {
    api.fetchReviewEvidenceContent.mockImplementation(async (id: string) => {
      if (id === "ev-trace") throw new Error("trace expired");
      return new Blob(["png"]);
    });
    render(<BrowserRegressionResult check={check(run({ artifacts: { screenshot: artifact("ev-after"), trace: artifact("ev-trace", "application/zip") } }), null)} />);
    await expand();

    await userEvent.click(await screen.findByRole("button", { name: "After trace" }));
    expect(await screen.findByText("trace expired")).toBeTruthy();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });
});
