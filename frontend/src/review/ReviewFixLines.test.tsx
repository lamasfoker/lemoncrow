import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewFixLines, { fixLineChip } from "./ReviewFixLines";
import type { FixLine } from "./types";

const api = vi.hoisted(() => ({
  startFixLine: vi.fn(),
  planFixApply: vi.fn(),
  applyFixLine: vi.fn(),
  discardFixLine: vi.fn(),
  resolveFixLine: vi.fn(),
  recreateFixWorktree: vi.fn(),
}));

vi.mock("./reviewApi", () => ({
  historicalReviewHref: (reviewId: string, revisionNumber: number) =>
    `/r/${reviewId}?base=${revisionNumber}&target=${revisionNumber}`,
  startFixLine: api.startFixLine,
  planFixApply: api.planFixApply,
  applyFixLine: api.applyFixLine,
  discardFixLine: api.discardFixLine,
  resolveFixLine: api.resolveFixLine,
  recreateFixWorktree: api.recreateFixWorktree,
}));

const line: FixLine = {
  id: "fix1",
  review_id: "parent",
  snapshot_revision_id: "rev3",
  snapshot_number: 3,
  child_review_id: "child",
  status: "fixing",
  worktree_path: "/work/repo/.lemoncrow/fixes/k7q2-s3",
  branch: "lc-fix/k7q2-s3",
  base_commit: "abc",
  worktree_missing: false,
  applied_revision_id: "",
  applied_files: [],
  applied_at: "",
  snapshots: [{ id: "c1", fix_number: 1, display_number: 0, created_at: "" }],
  checklist: [],
  unreviewed: 0,
};

describe("ReviewFixLines", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("starts a fix line from a Snapshot", async () => {
    api.startFixLine.mockResolvedValue({ fix_line: line, created: true });
    const changed = vi.fn();
    render(
      <ReviewFixLines
        reviewId="parent"
        revisionId="rev3"
        snapshotNumber={3}
        line={null}
        canFix
        onChanged={changed}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Fix in worktree" }));

    expect(api.startFixLine).toHaveBeenCalledWith("parent", "rev3");
    await waitFor(() => expect(changed).toHaveBeenCalled());
  });

  it("stops on conflicts and applies markers only after confirmation", async () => {
    api.planFixApply.mockResolvedValue({
      fix_line_id: "fix1",
      revision_id: "c1",
      fix_number: 1,
      files: [{ path: "app.py", outcome: "conflict", reason: "overlap" }],
      conflicts: ["app.py"],
      warnings: [],
    });
    api.applyFixLine.mockResolvedValue({
      applied: true,
      fix_line: { ...line, status: "applied_with_conflicts" },
      plan: {
        fix_line_id: "fix1",
        revision_id: "c1",
        fix_number: 1,
        files: [{ path: "app.py", outcome: "conflict", reason: "overlap" }],
        conflicts: ["app.py"],
        warnings: [],
      },
    });
    const changed = vi.fn();
    render(
      <ReviewFixLines
        reviewId="parent"
        revisionId="rev3"
        snapshotNumber={3}
        line={line}
        canFix
        onChanged={changed}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Apply fix to main" }));
    expect(await screen.findByText(/Nothing written/)).toBeTruthy();
    expect(api.applyFixLine).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole("button", { name: "Apply with conflict markers" }),
    );

    expect(api.applyFixLine).toHaveBeenCalledWith("parent", "fix1", true);
    await waitFor(() => expect(changed).toHaveBeenCalled());
  });

  it("requires confirmation for warnings even without conflicts", async () => {
    api.planFixApply.mockResolvedValue({
      fix_line_id: "fix1",
      revision_id: "c1",
      fix_number: 1,
      files: [{ path: "app.py", outcome: "write", reason: "" }],
      conflicts: [],
      warnings: ["1 comment is still open"],
    });
    api.applyFixLine.mockResolvedValue({
      applied: true,
      fix_line: { ...line, status: "applied" },
      plan: {
        fix_line_id: "fix1",
        revision_id: "c1",
        fix_number: 1,
        files: [],
        conflicts: [],
        warnings: [],
      },
    });
    render(
      <ReviewFixLines
        reviewId="parent"
        revisionId="rev3"
        snapshotNumber={3}
        line={line}
        canFix
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Apply fix to main" }));
    expect(await screen.findByText("1 comment is still open")).toBeTruthy();
    expect(api.applyFixLine).not.toHaveBeenCalled();

    const buttons = screen.getAllByRole("button", { name: "Apply fix to main" });
    await userEvent.click(buttons[buttons.length - 1]);
    expect(api.applyFixLine).toHaveBeenCalledWith("parent", "fix1", false);
  });

  it("confirms before discarding uncommitted fix work", async () => {
    api.discardFixLine
      .mockResolvedValueOnce({
        closed: false,
        dirty_files: ["late.txt"],
        fix_line: line,
      })
      .mockResolvedValueOnce({
        closed: true,
        dirty_files: [],
        fix_line: { ...line, status: "abandoned" },
      });
    render(
      <ReviewFixLines
        reviewId="parent"
        revisionId="rev3"
        snapshotNumber={3}
        line={line}
        canFix
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Discard fix line" }));
    expect(await screen.findByText("late.txt")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Discard anyway" }));

    expect(api.discardFixLine).toHaveBeenLastCalledWith("parent", "fix1", true);
  });

  it("names every state", () => {
    expect(fixLineChip({ ...line, status: "applied" })).toBe("Applied to main");
    expect(fixLineChip({ ...line, status: "applied_with_conflicts" })).toBe(
      "Applied with conflicts",
    );
    expect(fixLineChip({ ...line, status: "abandoned" })).toBe("Abandoned");
    expect(fixLineChip({ ...line, worktree_missing: true })).toBe("Worktree missing");
  });
});
