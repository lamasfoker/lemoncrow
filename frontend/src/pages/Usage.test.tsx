import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import {
  LOCAL_PRODUCT_CAPABILITIES,
  ProductCapabilitiesProvider,
  createProductCapabilities,
  type LemonCrowCapabilities,
} from "../lib/RuntimeCapabilities";

vi.mock("./Analytics", () => ({ default: () => <div>Analytics body</div> }));
vi.mock("../review/analytics/ReviewAnalytics", () => ({ default: () => <div>Review analytics body</div> }));
vi.mock("./Savings", () => ({ default: () => <div>Savings body</div> }));
vi.mock("./Optimizations", () => ({ default: () => <div>Optimizations body</div> }));
vi.mock("./Reports", () => ({ default: () => <div>Reports body</div> }));

import Usage from "./Usage";

const HOSTED = createProductCapabilities({
  review: true,
  runtime: true,
  mode: "hosted",
  runtimeCapabilities: {
    localMachine: false,
    localIntegrations: false,
    localEditor: false,
    liveWorkspace: false,
    localReviewCapture: false,
    codeAsk: false,
    swarms: false,
    localDiagnostics: false,
    knowledge: false,
  },
});

async function openInspectorViews(path: string, capabilities: LemonCrowCapabilities) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <ProductCapabilitiesProvider value={capabilities}>
        <Usage />
      </ProductCapabilitiesProvider>
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("combobox", { name: "Usage inspector view" }));
}

describe("Usage inspector", () => {
  it("offers the advisor and benchmarks locally", async () => {
    await openInspectorViews("/usage?inspect=savings", LOCAL_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("option", { name: "Optimization advisor" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Benchmarks" })).toBeInTheDocument();
  });

  it("hides the local-only advisor and benchmarks where the server lacks them", async () => {
    await openInspectorViews("/usage?inspect=savings", HOSTED);

    expect(await screen.findByRole("option", { name: "Savings evidence" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Optimization advisor" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Benchmarks" })).not.toBeInTheDocument();
  });

  it("falls back to savings evidence for a bookmarked advisor link", async () => {
    render(
      <MemoryRouter initialEntries={["/usage?inspect=advisor"]}>
        <ProductCapabilitiesProvider value={HOSTED}>
          <Usage />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Savings body")).toBeInTheDocument();
    expect(screen.queryByText("Optimizations body")).not.toBeInTheDocument();
  });
});
