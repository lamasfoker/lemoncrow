import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ReviewSnapshotControl from "./ReviewSnapshotControl";

describe("ReviewSnapshotControl", () => {
  it("captures directly and keeps comparison, history, and hide in its dropdown", async () => {
    const onCapture = vi.fn();
    const onHistory = vi.fn();
    const onHide = vi.fn();
    render(<ReviewSnapshotControl files={465} detail="465 files changed after Snapshot 355." compareHref="/r/review/compare/worktree" busy={false} onCapture={onCapture} onHistory={onHistory} onHide={onHide} />);
    const trigger = screen.getByRole("button", { name: "New Snapshot options" });
    expect(screen.queryByRole("menuitem")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Capture current source" }));
    expect(onCapture).toHaveBeenCalledTimes(1);
    await userEvent.click(trigger);
    expect(screen.getByRole("menuitem", { name: "Compare source" })).toHaveAttribute("href", "/r/review/compare/worktree");
    expect(screen.getByText("465 files changed after Snapshot 355.")).toBeTruthy();
    await userEvent.click(screen.getByRole("menuitem", { name: "Snapshots" }));
    expect(onHistory).toHaveBeenCalledTimes(1);
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("menuitem", { name: "Hide until source changes again" }));
    expect(onHide).toHaveBeenCalledTimes(1);
  });

  it("blocks capturing while busy without offering hide for pending reviewer edits", async () => {
    const onCapture = vi.fn();
    render(<ReviewSnapshotControl files={1} detail="1 edit awaits capture." compareHref="/compare" busy onCapture={onCapture} onHistory={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Capture current source" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "New Snapshot options" }));
    expect(screen.queryByRole("menuitem", { name: /Hide/ })).toBeNull();
    expect(onCapture).not.toHaveBeenCalled();
  });
});
