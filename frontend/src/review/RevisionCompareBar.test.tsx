import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import RevisionCompareBar from "./RevisionCompareBar";

describe("RevisionCompareBar", () => {
  it("names both Snapshots of the comparison and exits on request", async () => {
    const onExit = vi.fn();
    render(<RevisionCompareBar baseRevision={2} targetRevision={5} onExit={onExit} />);

    const bar = screen.getByTestId("revision-compare-bar");
    expect(bar.getAttribute("role")).toBe("status");
    expect(screen.getByText("Comparing Snapshot 2 → Snapshot 5")).toBeTruthy();
    expect(screen.queryByText("Loading comparison…")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Show full review" }));
    expect(onExit).toHaveBeenCalledTimes(1);
  });

  it("labels a comparison against the current source", () => {
    render(<RevisionCompareBar baseRevision="head" targetRevision={3} onExit={vi.fn()} />);
    expect(screen.getByText("Comparing Source → Snapshot 3")).toBeTruthy();
  });

  it("shows progress while loading and the failure once loading stops", () => {
    const { rerender } = render(
      <RevisionCompareBar baseRevision={1} targetRevision={2} loading error="stale error" onExit={vi.fn()} />,
    );
    expect(screen.getByText("Loading comparison…")).toBeTruthy();
    expect(screen.queryByText("stale error")).toBeNull();

    rerender(<RevisionCompareBar baseRevision={1} targetRevision={2} error="Snapshot 1 is unavailable" onExit={vi.fn()} />);
    expect(screen.queryByText("Loading comparison…")).toBeNull();
    expect(screen.getByText("Snapshot 1 is unavailable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Show full review" })).toBeTruthy();
  });
});
