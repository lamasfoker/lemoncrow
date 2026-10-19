import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ReviewFixOfBanner from "./ReviewFixOfBanner";

const api = vi.hoisted(() => ({ fetchFixLines: vi.fn() }));
vi.mock("./reviewApi", () => ({
  fetchFixLines: api.fetchFixLines,
  reviewHref: (id: string) => `/r/${id}`,
}));

vi.mock("./ReviewFixLines", () => ({
  fixLineChip: () => "Fixing · 1 snapshot",
}));

describe("ReviewFixOfBanner", () => {
  it("names the Snapshot a fix Review fixes", async () => {
    api.fetchFixLines.mockResolvedValue({
      fix_lines: [],
      fix_of: {
        id: "fix1",
        review_id: "parent",
        snapshot_number: 3,
        status: "fixing",
        worktree_missing: false,
        snapshots: [
          { id: "c1", fix_number: 1, display_number: 0, created_at: "" },
        ],
      },
    });

    render(<ReviewFixOfBanner reviewId="child" />);

    expect(await screen.findByText("Fix of Snapshot 3")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Back to the Review" }).getAttribute("href"),
    ).toBe("/r/parent");
  });

  it("renders nothing for an ordinary Review", async () => {
    api.fetchFixLines.mockResolvedValue({ fix_lines: [], fix_of: null });
    const { container } = render(<ReviewFixOfBanner reviewId="parent" />);

    await waitFor(() => expect(api.fetchFixLines).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});
