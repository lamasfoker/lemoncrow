import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewFeedbackPanel from "./ReviewFeedbackPanel";
import type { FeedbackExport } from "./types";

const feedback: FeedbackExport = {
  markdown: "## Review feedback\n\nPlease fix this.",
  open: 1,
  orphaned: 0,
  resolved: 0,
  revision_id: "rrv-1",
  feedback_hash: "feedback-hash-1",
  operation_id: "fop-1",
  annotation_versions: { "ann-1": 1 },
  delivery: {
    supported: false,
    host: "",
    session_id: "",
    target_ref: "",
    label: "",
    reason: "Direct delivery unavailable",
  },
  status: {
    open_total: 1,
    unpublished: 1,
    published: 0,
    in_flight: 0,
    addressed: 0,
  },
};

describe("ReviewFeedbackPanel", () => {
  it("confirms when the fallback bundle was copied", async () => {
    const onCopy = vi.fn(async () => {});

    const { rerender } = render(
      <ReviewFeedbackPanel
        feedback={feedback}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Copy review feedback bundle" }));
    await waitFor(() => expect(onCopy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Copied")).toBeTruthy();

    rerender(
      <ReviewFeedbackPanel
        feedback={{ ...feedback, operation_id: "fop-2" }}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByText("Copy feedback")).toBeTruthy();
  });

  it("does not show stale unsent actions after delivery succeeds", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          delivery: {
            supported: true,
            host: "claude",
            session_id: "session-1",
            target_ref: "claude:session-1",
            label: "Claude",
            reason: "",
          },
        }}
        delivery={{
          state: "sent",
          target_ref: "claude:session-1",
          remote_ref: "message-1",
          message: "sent",
          annotation_count: 1,
          operation_id: "fop-1",
        }}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Sent to Claude")).toBeTruthy();
    expect(screen.queryByText("Consumed by Claude")).toBeNull();
    expect(screen.queryByText("1 to send")).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy review feedback bundle" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send 1 to Claude" })).toBeNull();
  });

  it("claims consumption only when the exact session acknowledged it", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          delivery: {
            supported: true,
            host: "claude",
            session_id: "session-1",
            target_ref: "claude:session-1",
            label: "Claude",
            reason: "",
          },
        }}
        delivery={{
          state: "consumed",
          target_ref: "claude:session-1",
          remote_ref: "message-1",
          message: "consumed",
          annotation_count: 1,
          operation_id: "fop-1",
        }}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Consumed by Claude")).toBeTruthy();
    expect(screen.queryByText(/Delivery consumed:/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Send 1 to Claude" })).toBeNull();
  });

  it("does not offer resend when delivery is uncertain", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          delivery: {
            supported: true,
            host: "claude",
            session_id: "session-1",
            target_ref: "claude:session-1",
            label: "Claude",
            reason: "",
          },
          status: { open_total: 1, unpublished: 0, published: 0, in_flight: 1, addressed: 0 },
        }}
        delivery={{
          state: "uncertain",
          target_ref: "claude:session-1",
          remote_ref: "job-1",
          message: "Claude started but verification failed",
          annotation_count: 1,
          operation_id: "fop-1",
        }}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Delivery needs inspection")).toBeTruthy();
    expect(screen.getByText(/Do not resend this prepared operation/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send 1 to Claude" })).toBeNull();
  });

  it("stops reporting the agent as working once it marked every handed-off comment addressed", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          annotation_versions: { "ann-1": 1, "ann-2": 1, "ann-3": 1 },
          delivery: {
            supported: true,
            host: "codex",
            session_id: "session-1",
            target_ref: "codex:session-1",
            label: "Codex",
            reason: "",
          },
          status: { open_total: 3, unpublished: 0, published: 3, in_flight: 3, addressed: 0 },
        }}
        delivery={{
          state: "working",
          target_ref: "codex:session-1",
          remote_ref: "turn-1",
          message: "working",
          annotation_count: 3,
          operation_id: "fop-1",
        }}
        handoffRereview={3}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Re-review addressed feedback")).toBeTruthy();
    expect(screen.getByText("3 re-review")).toBeTruthy();
    expect(screen.queryByText("Working in Codex")).toBeNull();
    expect(screen.queryByText(/Delivery working:/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Send / })).toBeNull();
  });

  it("keeps manual copy recovery visible when clipboard copy fails", async () => {
    const onCopy = vi.fn(async () => {
      throw new Error("clipboard denied");
    });

    render(
      <ReviewFeedbackPanel
        feedback={feedback}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Copy review feedback bundle" }));

    expect(await screen.findByText(/Clipboard copy failed/)).toBeTruthy();
    expect(screen.getByText(/Open the preview below and copy the feedback manually/)).toBeTruthy();
    expect(screen.getByText("Preview feedback")).toBeTruthy();
  });

  it("copies for the author and records the handoff only after the copy succeeds", async () => {
    const onCopy = vi.fn(async () => {});
    const onSend = vi.fn();
    const toAuthor = {
      ...feedback,
      delivery: { ...feedback.delivery, supported: true, kind: "export" as const, label: "author", target_ref: "clipboard:author" },
    };
    render(
      <ReviewFeedbackPanel feedback={toAuthor} delivery={null} busy={false} canDirectSend onCopy={onCopy} onSend={onSend} />,
    );
    expect(screen.getByText("Copy feedback for the author")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Copy review feedback bundle" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Copy 1 for the author" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith({ copied: true }));
    expect(onCopy.mock.invocationCallOrder[0]).toBeLessThan(onSend.mock.invocationCallOrder[0]);
  });

  it("does not record a copy for the author when the clipboard refuses", async () => {
    const onSend = vi.fn();
    const toAuthor = {
      ...feedback,
      delivery: { ...feedback.delivery, supported: true, kind: "export" as const, label: "author", target_ref: "clipboard:author" },
    };
    render(
      <ReviewFeedbackPanel
        feedback={toAuthor}
        delivery={null}
        busy={false}
        canDirectSend
        onCopy={async () => { throw new Error("denied"); }}
        onSend={onSend}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Copy 1 for the author" }));
    expect(await screen.findByText(/Clipboard copy failed/)).toBeTruthy();
    expect(onSend).not.toHaveBeenCalled();

    // Without clipboard access the reviewer copies by hand; only their confirmation records it.
    expect(screen.getByText(/Copy the preview below yourself, then confirm/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "I copied it" }));
    expect(onSend).toHaveBeenCalledWith({ copied: true });
  });

  it("publishes a pull-request route through the GitHub preview instead of sending", async () => {
    const onPublishGitHub = vi.fn();
    const toPr = {
      ...feedback,
      delivery: { ...feedback.delivery, supported: true, kind: "github" as const, label: "PR #7", target_ref: "github:acme/app#7" },
      status: { ...feedback.status!, github: { unpublished: 1, published: 0, in_flight: 0 } },
    };
    render(
      <ReviewFeedbackPanel feedback={toPr} delivery={null} busy={false} canDirectSend onCopy={vi.fn()} onSend={vi.fn()} onPublishGitHub={onPublishGitHub} />,
    );
    expect(screen.queryByRole("button", { name: /^Send / })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Publish 1 to PR #7…" }));
    expect(onPublishGitHub).toHaveBeenCalledTimes(1);
  });

  describe("send eligibility per delivery state (stale unpublished count still > 0)", () => {
    const supported = {
      ...feedback,
      delivery: {
        supported: true, host: "claude", session_id: "s1", target_ref: "claude:s1", label: "Claude", reason: "",
      },
    };
    const mk = (state: string, remote_ref = "") => ({
      state, target_ref: "claude:s1", remote_ref, message: "m", annotation_count: 1, operation_id: "fop-1",
    }) as never;
    const cases: [string, string, string, boolean][] = [
      ["prepared", "prepared", "", true],
      ["queued", "queued", "", false],
      ["leased", "leased", "", false],
      ["dispatching", "dispatching", "", false],
      ["working", "working", "", false],
      ["sent", "sent", "", false],
      ["consumed", "consumed", "", false],
      ["partial", "partial", "", false],
      ["blocked without remote ref", "blocked", "", true],
      ["blocked with remote ref", "blocked", "job-1", false],
      ["failed without remote ref", "failed", "", true],
      ["failed with remote ref", "failed", "job-1", false],
      ["uncertain", "uncertain", "", false],
    ];
    it.each(cases)("%s", (_label, state, ref, canSend) => {
      render(
        <ReviewFeedbackPanel
          feedback={supported}
          delivery={mk(state, ref)}
          busy={false}
          canDirectSend
          onCopy={vi.fn()}
          onSend={vi.fn()}
          onClose={vi.fn()}
        />,
      );
      expect(Boolean(screen.queryByRole("button", { name: "Send 1 to Claude" }))).toBe(canSend);
    });
  });
});
