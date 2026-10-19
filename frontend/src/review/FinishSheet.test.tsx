import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import FinishSheet from "./FinishSheet";
import type { ReviewClosure } from "./types";

function summary(overrides: Partial<ReviewClosure> = {}): ReviewClosure {
  return {
    status: "open",
    target_count: 10,
    reviewed_targets: 6,
    unreviewed_targets: 1,
    changed_since_review: 1,
    needs_changes: 1,
    unknown_targets: 1,
    open_comments: 2,
    orphaned_comments: 1,
    failed_verification: 1,
    unresolved_verification: 2,
    previous_revision_evidence: 3,
    discarded_verdicts: 1,
    ...overrides,
  };
}

describe("FinishSheet", () => {
  it("keeps Finish focused and routes detailed readiness to Review Status", async () => {
    const onOpenStatus = vi.fn();
    render(
      <FinishSheet
        summary={summary({
          decisions: {
            total: 3,
            open: 1,
            change_requested: 0,
            deferred: 1,
            needs_reconsideration: 0,
            implementation_changed: 0,
            unverified: 1,
            accepted: 0,
          },
          evidence_gaps: {
            total: 1,
            claims: [{ id: "claim:a", item_id: "item:a", title: "Branch identity", summary: "none", summary_text: "No evidence" }],
          },
        })}
        degraded={[{ name: "impact", note: "Index unavailable" }]}
        evidenceFacts={[{ id: "unit", label: "Unit tests" }]}
        busy={false}
        onOpenStatus={onOpenStatus}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    expect(screen.getByText("Your verdict")).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Review outcome summary" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Review status summary" })).toHaveTextContent("6/10 reviewed");
    expect(screen.queryByText("Decisions")).toBeNull();
    expect(screen.queryByText("Verification evidence")).toBeNull();
    expect(screen.queryByText("Evidence gaps")).toBeNull();
    expect(screen.queryByText("Analysis limitations")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Review Status" }));
    expect(onOpenStatus).toHaveBeenCalledTimes(1);
  });

  it("uses a plain finish action only when the authoritative summary has no blockers", () => {
    render(
      <FinishSheet
        summary={summary({
          target_count: 10,
          reviewed_targets: 10,
          unreviewed_targets: 0,
          changed_since_review: 0,
          needs_changes: 0,
          unknown_targets: 0,
          open_comments: 0,
          orphaned_comments: 0,
          failed_verification: 0,
          unresolved_verification: 0,
        })}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Finish" })).toBeTruthy();
    expect(screen.getByText(/no current blockers/i)).toBeTruthy();
  });

  it("does not mutate until the explicit finish action is pressed", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("records the selected verdict and optional summary only on Finish", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={vi.fn()} onFinish={onFinish} />);

    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ship it.");
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "LGTM & finish anyway" }));
    expect(onFinish).toHaveBeenCalledWith("lgtm", "Ship it.", { send: false });
  });

  it("does not silently carry an older-Snapshot verdict into the current choice", () => {
    render(
      <FinishSheet
        summary={summary()}
        currentOutcome={{
          id: "rot-old",
          review_id: "rev-1",
          revision_id: "rrv-old",
          reviewer_id: "alice",
          outcome: "lgtm",
          summary: "Old LGTM",
          created_at: "2026-09-17T00:00:00Z",
          stale: true,
        }}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByText(/applies to an older Snapshot/i)).toBeTruthy();
    expect(screen.getByRole("radio", { name: "Comment" })).toHaveAttribute("aria-checked", "true");
  });

  it("focuses its close control, traps focus and supports Escape", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    const close = screen.getByRole("button", { name: "Close review outcome" });
    const last = screen.getByRole("button", { name: "Finish anyway" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("holds actions while Finish is in flight", () => {
    render(<FinishSheet summary={summary()} busy onClose={vi.fn()} onFinish={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Recording…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Continue reviewing" })).toBeDisabled();
  });
});

describe("FinishSheet pending feedback", () => {
  const claude = { unsent: 2, label: "Claude a1b2", kind: "agent" as const, available: true, reason: "" };

  it("keeps iterative Send feedback separate while allowing an explicit Finish delivery choice", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} handoff={claude} onClose={vi.fn()} onFinish={onFinish} />);

    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("region", { name: "Where fixes go" })).toHaveTextContent("Pending feedback");
    expect(screen.getByRole("region", { name: "Where fixes go" })).toHaveTextContent("2 comments");
    await userEvent.click(screen.getByRole("button", { name: "Request changes & send 2" }));
    expect(onFinish).toHaveBeenCalledWith("changes_requested", "", { send: true });
  });

  it("requires something actionable before recording Request changes with no pending feedback", async () => {
    const onFinish = vi.fn();
    render(
      <FinishSheet
        summary={summary({ open_comments: 0 })}
        busy={false}
        handoff={{ ...claude, unsent: 0 }}
        onClose={vi.fn()}
        onFinish={onFinish}
      />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    expect(screen.getByText("Add a comment or a summary for the fixer.")).toBeTruthy();
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Handle the empty list.");
    await userEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(onFinish).toHaveBeenCalledWith("changes_requested", "Handle the empty list.", { send: false });
  });

  it("refuses an unavailable delivery and says why", async () => {
    render(
      <FinishSheet
        summary={summary()}
        busy={false}
        handoff={{ ...claude, available: false, reason: "direct feedback delivery is not configured for gemini" }}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("button", { name: "Request changes & send 2" })).toBeDisabled();
    expect(screen.getByText(/not configured for gemini/)).toBeTruthy();
  });

  it("can finish without sending pending feedback for non-request-change outcomes", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} handoff={claude} onClose={vi.fn()} onFinish={onFinish} />);
    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    const also = screen.getByRole("checkbox", { name: "Also send 2 to Claude a1b2" });
    expect(also).toBeChecked();
    await userEvent.click(also);
    await userEvent.click(screen.getByRole("button", { name: "LGTM & finish anyway" }));
    expect(onFinish).toHaveBeenCalledWith("lgtm", "", { send: false });
  });

  it("after failed delivery offers Retry and Finish without sending", async () => {
    const onFinish = vi.fn();
    render(
      <FinishSheet summary={summary()} busy={false} handoff={claude} sendError="session refused" onClose={vi.fn()} onFinish={onFinish} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was finished; the comments were not sent: session refused");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onFinish).toHaveBeenLastCalledWith("comment", "", { send: true });
    await userEvent.click(screen.getByRole("button", { name: "Finish without sending" }));
    expect(onFinish).toHaveBeenLastCalledWith("comment", "", { send: false });
  });
});
