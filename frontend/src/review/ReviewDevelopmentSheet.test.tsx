import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ContextDevelopment from "./ContextDevelopment";
import ReviewDevelopmentSheet, { groupTimeline, hostLabel } from "./ReviewDevelopmentSheet";
import { development, event, projection } from "./developmentTestFixtures";

const api = vi.hoisted(() => ({ fetchReviewDevelopmentEvents: vi.fn() }));
const sessions = vi.hoisted(() => ({ ledger: vi.fn() }));
vi.mock("./reviewApi", () => ({ fetchReviewDevelopmentEvents: api.fetchReviewDevelopmentEvents }));
vi.mock("../api", () => ({ api: sessions }));
const approvalsApi = vi.hoisted(() => ({ fetchAgentApprovals: vi.fn(), decideAgentApproval: vi.fn() }));
vi.mock("./agentApprovalsApi", () => approvalsApi);

describe("ReviewDevelopmentSheet", () => {
  beforeEach(() => {
    api.fetchReviewDevelopmentEvents.mockReset();
    api.fetchReviewDevelopmentEvents.mockResolvedValue({ events: [event("e1")], next_before: null });
    sessions.ledger.mockReset();
    sessions.ledger.mockResolvedValue({ conversations: [
      { kind: "agent_message", at: "2026-09-27T10:00:00Z", summary: "Update", content: "Authentication tests now pass." },
    ] });
  });

  it("renders the server projection: summary, attention, contributors, verification and feedback", async () => {
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "Activity" });
    expect(within(dialog).getByText(/Snapshot 52 · frozen with this Snapshot/)).toBeTruthy();
    expect(within(dialog).getByText("Session expiry now rejects stale tokens; focused auth tests pass.")).toBeTruthy();
    expect(within(dialog).getByText("3 files · 42 checks passed · 1 failed")).toBeTruthy();

    const attention = within(dialog).getByRole("heading", { name: "Reviewer attention" }).closest("section")!;
    expect(within(attention).getByText("uv run mypy src")).toBeTruthy();
    expect(within(attention).getByText("Should mobile keep legacy cookies?")).toBeTruthy();
    expect(within(attention).getByText(/1 changed file with no recorded author/)).toBeTruthy();

    const sessions = within(dialog).getByRole("heading", { name: "Agent sessions" }).closest("section")!;
    const rows = within(sessions).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("Claude session");
    expect(rows[0].textContent).toContain("Driver: Claude Code");
    expect(rows[0].textContent).toContain("Model: claude-opus");
    expect(rows[0].textContent).toContain("author");
    expect(rows[2].textContent).toContain("1 check failed");
    expect(within(rows[0]).getByRole("button", { name: "Open Claude session c1" })).toBeTruthy();
    expect(within(rows[0]).queryByRole("link")).toBeNull();
    expect(within(rows[2]).queryByRole("button", { name: /Open.*session/ })).toBeNull();

    await userEvent.click(within(dialog).getByRole("button", { name: "Show checks, decisions, files and activity" }));
    expect(within(dialog).getByText("Harden session expiry")).toBeTruthy();
    expect(within(dialog).getByText("Claude addressed 2 comments")).toBeTruthy();
    expect(within(dialog).getByText(/1 file changed · 4 checks passed · included in Snapshot 53/)).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenCalledWith("review-1", expect.objectContaining({ revisionNumber: 52, scope: "snapshot", view: "all" })));
  });

  it("keeps newer work separate from the frozen Snapshot and switches the timeline scope", async () => {
    const since = projection({
      scope: "since",
      headline: "",
      contributors: [{ ...projection().contributors[1], active: true }],
      verification: { pass: 3, fail: 0, pending: 0, checks: [] },
      newer_paths: ["src/retry.py", "src/backoff.py"],
      active: true,
      event_count: 6,
      feedback: [],
    });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ since })} historical={false} onClose={vi.fn()} />);

    const band = screen.getByRole("status");
    expect(band.textContent).toContain("Codex · 2 newer files · 3 passed");
    expect(screen.getByText("3 files · 42 checks passed · 1 failed")).toBeTruthy();
    const sessions = screen.getByRole("heading", { name: "Agent sessions" }).closest("section")!;
    expect(within(sessions).getAllByRole("listitem")[0].textContent).toContain("Codex session");
    expect(within(sessions).getAllByRole("listitem")[0].textContent).toContain("Working now");

    await userEvent.click(within(band as HTMLElement).getByRole("button", { name: "View newer activity" }));
    expect(screen.getByRole("button", { name: "Hide checks, decisions, files and activity" }).getAttribute("aria-expanded")).toBe("true");
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ scope: "since" })));
  });

  it("filters, pages and collapses repetitive progress updates", async () => {
    api.fetchReviewDevelopmentEvents
      .mockResolvedValueOnce({
        events: [
          event("e9", { kind: "message", summary: "Tests pass now.", path: "", status: "" }),
          event("e8", { kind: "message", summary: "Editing auth.", path: "", status: "" }),
          event("e7", { kind: "message", summary: "Reading callers.", path: "", status: "" }),
          event("e6", { kind: "test", summary: "pytest -q", status: "pass", path: "" }),
        ],
        next_before: 6,
      })
      .mockResolvedValueOnce({ events: [event("e2", { summary: "Edited src/session.py", path: "src/session.py" })], next_before: null })
      .mockResolvedValue({ events: [event("e6", { kind: "test", summary: "pytest -q", status: "pass", path: "" })], next_before: null });

    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);
    expect(api.fetchReviewDevelopmentEvents).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    expect(await screen.findByText("3 progress updates from Claude")).toBeTruthy();
    expect(screen.getByText("pytest -q", { selector: "span" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Load earlier activity" }));
    expect(await screen.findByText("Edited src/session.py")).toBeTruthy();
    expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ before: 6 }));

    await userEvent.click(screen.getByRole("radio", { name: "Verification" }));
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ view: "verification", before: null })));
  });

  it("keeps the earlier pages a reader loaded when live activity arrives", async () => {
    const page = (from: number) => Array.from({ length: 40 }, (_, index) => event(`e${from - index}`, { summary: `Edit ${from - index}` }));
    api.fetchReviewDevelopmentEvents
      .mockResolvedValueOnce({ events: page(100), next_before: 61 })
      .mockResolvedValueOnce({ events: page(60), next_before: 21 })
      .mockResolvedValue({ events: [event("e101", { summary: "Edit 101" }), ...page(100), ...page(60)].slice(0, 80), next_before: 22 });

    const { rerender } = render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    await userEvent.click(await screen.findByRole("button", { name: "Load earlier activity" }));
    expect(await screen.findByText("Edit 21")).toBeTruthy();

    rerender(<ReviewDevelopmentSheet reviewId="review-1" development={development({ cursor: "13" })} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("Edit 101")).toBeTruthy();
    expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ before: null, limit: 80 }));
    expect(screen.getByText("Edit 22")).toBeTruthy();
  });

  it("explains an empty revision honestly and closes on Escape", async () => {
    const onClose = vi.fn();
    render(
      <ReviewDevelopmentSheet
        reviewId="review-1"
        development={development({
          snapshot: projection({ available: false, contributors: [], event_count: 0, headline: "", intent: "" }),
          reason: "No exact development evidence is attached to this Snapshot yet.",
        })}
        historical
        onClose={onClose}
      />,
    );
    expect(screen.getByText("No exact development evidence is attached to this Snapshot yet.")).toBeTruthy();
    expect(screen.getByText(/Snapshot 52 · frozen historical record/)).toBeTruthy();
    expect(api.fetchReviewDevelopmentEvents).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("opens a changed path and closes itself", async () => {
    const onClose = vi.fn();
    const onOpenPath = vi.fn();
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={onClose} onOpenPath={onOpenPath} />);
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    const changed = screen.getByRole("heading", { name: "Changed during development" }).closest("section")!;
    await userEvent.click(within(changed).getByRole("button", { name: "src/cache.py" }));
    expect(onClose).toHaveBeenCalled();
    expect(onOpenPath).toHaveBeenCalledWith("src/cache.py");
    expect(within(changed).getByText("2 agents")).toBeTruthy();
  });

  it("keeps the server narrative available in evidence", async () => {
    const narrative = [
      "Claude and Codex changed 3 files — Claude: auth.py and session.py; Codex: cache.py.",
      "1 of 43 checks failed (run by Claude and CI).",
    ];
    render(
      <ReviewDevelopmentSheet
        reviewId="review-1"
        development={development({ snapshot: projection({ narrative }) })}
        historical={false}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("development-narrative")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    expect(screen.getByTestId("development-narrative").textContent).toBe(narrative.join(" "));
  });
  it("reads the session in place and restores evidence, scroll and focus on return", async () => {
    const onClose = vi.fn();
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    const evidence = screen.getByTestId("development-evidence");
    const scrollArea = evidence.parentElement!;
    scrollArea.scrollTop = 320;
    const trigger = screen.getByRole("button", { name: "Open Claude session c1" });
    await userEvent.click(trigger);

    const conversation = screen.getByRole("region", { name: "Agent session conversation" });
    expect(await within(conversation).findByText("Authentication tests now pass.")).toBeTruthy();
    expect(sessions.ledger).toHaveBeenCalledWith("c1");
    expect(screen.queryByRole("heading", { name: "Agent sessions" })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Back to Activity" })).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: "Back to Activity" }));
    expect(screen.queryByRole("region", { name: "Agent session conversation" })).toBeNull();
    expect(screen.getByRole("button", { name: "Hide checks, decisions, files and activity" })).toHaveAttribute("aria-expanded", "true");
    expect(scrollArea.scrollTop).toBe(320);
    expect(trigger).toHaveFocus();
  });

  it("opens the exact source session from a failure, decision and timeline event", async () => {
    const snapshot = projection();
    snapshot.verification.checks[0] = { ...snapshot.verification.checks[0], host: "codex", session_id: "x1" };
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ snapshot })} historical={false} onClose={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Open Codex session x1 for uv run mypy src" }));
    expect(await screen.findByText("Authentication tests now pass.")).toBeTruthy();
    expect(sessions.ledger).toHaveBeenLastCalledWith("x1");
    expect(screen.getByText("Opened from: uv run mypy src")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Back to Activity" }));

    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    await userEvent.click(screen.getByRole("button", { name: "Open Claude session c1 for Rejected: cache session tokens" }));
    expect(await screen.findByText("Authentication tests now pass.")).toBeTruthy();
    expect(sessions.ledger).toHaveBeenLastCalledWith("c1");
    expect(screen.getByText("Opened from: Rejected: cache session tokens")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Back to Activity" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open Claude session c1 for Edited src/auth.py" }));
    expect(await screen.findByText("Authentication tests now pass.")).toBeTruthy();
    expect(sessions.ledger).toHaveBeenLastCalledWith("c1");
  });

  it("keeps keyboard focus in the conversation and Escape returns before closing", async () => {
    const onClose = vi.fn();
    sessions.ledger.mockResolvedValue({ conversations: [] });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={onClose} />);
    const trigger = screen.getByRole("button", { name: "Open Claude session c1" });
    await userEvent.click(trigger);
    expect(await screen.findByText("No conversation has been recorded for this session yet.")).toBeTruthy();
    screen.getByRole("button", { name: "Refresh session" }).focus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Close activity" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("retries a failed session load and refreshes its conversation", async () => {
    sessions.ledger.mockResolvedValueOnce({ error: "failed to load run ledger" })
      .mockResolvedValueOnce({ conversations: [] })
      .mockResolvedValueOnce({ conversations: [
        { kind: "agent_message", at: "", summary: "Update", content: "New session update" },
      ] });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Open Claude session c1" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No conversation has been recorded for this session yet.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Refresh session" }));
    expect(await screen.findByText("New session update")).toBeTruthy();
  });

  it("ignores a late response from a session that was closed", async () => {
    let resolveFirst!: (value: unknown) => void;
    sessions.ledger.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Open Claude session c1" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading conversation");
    await userEvent.click(screen.getByRole("button", { name: "Back to Activity" }));
    await userEvent.click(screen.getByRole("button", { name: "Open Codex session x1" }));
    expect(await screen.findByText("Authentication tests now pass.")).toBeTruthy();
    resolveFirst({ conversations: [{ kind: "agent_message", at: "", content: "Old session response" }] });
    await waitFor(() => expect(screen.queryByText("Old session response")).toBeNull());
    expect(screen.getByRole("heading", { name: "Codex session" })).toBeTruthy();
  });
});

describe("development helpers", () => {
  it("labels hosts and groups only consecutive updates from one session", () => {
    expect(hostLabel("opencode")).toBe("OpenCode");
    expect(hostLabel("lemoncrow:code")).toBe("LemonCrow");
    const rows = groupTimeline([
      event("e5", { kind: "message", session_id: "a" }),
      event("e4", { kind: "message", session_id: "a" }),
      event("e3", { kind: "message", session_id: "b" }),
      event("e2", { kind: "edit" }),
      event("e1", { kind: "message", session_id: "a" }),
    ]);
    expect(rows.map((row) => (row.type === "updates" ? `updates:${row.events.length}` : row.event.id))).toEqual([
      "updates:2",
      "e3",
      "e2",
      "e1",
    ]);
  });
});

describe("Agent actions outside the repository (Activity)", () => {
  const outside = [
    { id: "o2", category: "git_push", summary: "Git push: force push", detail: "git push --force origin main", host: "claude", session_id: "c1", role: "author", at: "2026-09-27T10:05:00Z", sequence: 9 },
    { id: "o1", category: "package_install", summary: "Package install: pip install requests", detail: "pip install requests", host: "codex", session_id: "x1", role: "author", at: "2026-09-27T10:01:00Z", sequence: 4 },
  ];

  beforeEach(() => {
    api.fetchReviewDevelopmentEvents.mockReset();
    api.fetchReviewDevelopmentEvents.mockResolvedValue({ events: [], next_before: null });
  });

  it("lists consequential actions in the Development sheet and the file Activity tab", async () => {
    const snapshot = projection({ outside_workspace: outside, counts: { ...projection().counts, outside_workspace: 2 } });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ snapshot })} historical={false} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    const section = screen.getByTestId("development-outside-workspace");
    expect(within(section).getByRole("heading", { name: /Outside the repository/ })).toBeTruthy();
    const rows = within(section).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Git push: force push"),
      expect.stringContaining("Package install: pip install requests"),
    ]);
    expect(within(rows[0]).getByText("Git push: force push").getAttribute("title")).toBe("git push --force origin main");
    cleanup();

    render(<ContextDevelopment reviewId="review-1" development={development({ snapshot })} path="src/auth.py" />);
    const context = screen.getByTestId("context-outside-workspace");
    expect(within(context).getAllByRole("listitem")).toHaveLength(2);
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenCalled());
  });

  it("says which actions a human pre-approved, lists the decisions and asks for pending ones (P12-AGENT)", async () => {
    const pending = {
      id: "apr_9", status: "pending" as const, used: false, risk_class: "dependency_install", risk_label: "Dependency install or upgrade",
      classes: ["dependency_install"], kind: "command", action: "npm install left-pad", action_digest: "e".repeat(64), cwd: "/w",
      host: "claude", session_id: "c1", requested_by: "agent:claude:c1", requested_at: "", expires_at: "", decided_by: "",
      decided_at: "", scope: "" as const, pattern: "", valid_until: "", claimed_at: "", note: "", revision_id: "rrv-1",
    };
    approvalsApi.fetchAgentApprovals.mockResolvedValue({ approvals: [pending], total: 1, policy: null, labels: {}, limits: {} });
    const labelled = [
      { ...outside[0], approval: { status: "unapproved" as const } },
      { ...outside[1], approval: { status: "pre_approved" as const, approval_id: "apr_1", decided_by: "alice", decided_at: "", scope: "once", pattern: "" } },
    ];
    const decisions = [
      { id: "ev-1", status: "denied", summary: "Denied dependency install or upgrade: npm install left-pad", detail: "Denied by alice", host: "claude", session_id: "c1", at: "", sequence: 11, action_digest: "e".repeat(64) },
    ];
    const snapshot = projection({ outside_workspace: labelled, approval_events: decisions, counts: { ...projection().counts, outside_workspace: 2 } });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ snapshot, agent_approvals: [pending] })} historical={false} onClose={vi.fn()} />);
    const panel = await screen.findByTestId("agent-approval-panel");
    expect(panel.textContent).toContain("npm install left-pad");
    expect(within(panel).getByRole("button", { name: "Approve once" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    const section = screen.getByTestId("development-outside-workspace");
    const rows = within(within(section).getByTestId("agent-actions")).getAllByRole("listitem");
    expect(rows[0].textContent).toContain("Not approved");
    expect(rows[1].textContent).toContain("Pre-approved by alice");
    expect(within(section).getByTestId("development-approval-decisions").textContent).toContain("Denied");
    cleanup();

    // A historical Snapshot shows the frozen decisions but asks nothing.
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ snapshot, agent_approvals: [pending] })} historical onClose={vi.fn()} />);
    expect(screen.queryByTestId("agent-approval-panel")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    expect(screen.getByTestId("development-approval-decisions").textContent).toContain("Denied");
  });

  it("adds no section when the agents did nothing outside the repository", async () => {
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Show checks, decisions, files and activity" }));
    expect(screen.getByTestId("development-evidence")).toBeTruthy();
    expect(screen.queryByTestId("development-outside-workspace")).toBeNull();
    cleanup();
    render(<ContextDevelopment reviewId="review-1" development={development()} path="src/auth.py" />);
    expect(screen.queryByTestId("context-outside-workspace")).toBeNull();
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenCalled());
  });
});
