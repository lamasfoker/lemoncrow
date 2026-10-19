import { Button as UiButton } from "../components/ui/button";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReaderHeader from "./ReaderHeader";
import { ReviewHandoffControls } from "./ReviewDestinationPicker";
import ReviewSnapshotControl from "./ReviewSnapshotControl";
import { authenticatedFetch } from "./reviewApi";
import { REVIEW_MAX_PERSISTENT_VIEWS, registeredHeaderControl } from "./reviewUxGate";
import { fallbackReviewEnvironment } from "./reviewEnvironment";
import type { ReviewOverview, ReviewProgress } from "./types";

vi.mock("./reviewApi", () => ({
  authenticatedFetch: vi.fn(async () => Response.json({ ok: true, delivered: "remote" })),
}));
const overview = {
  session: { id: "abcdef12abcdef127abcdef12abcdef12", ref: "r/abcdef12abcdef127abcdef12abcdef12", status: "open", actor_type: "agent", repo_root: "/server/scratch" },
  revision: { revision_number: 4, provenance_model: "claude-opus-5", provenance_certainty: "heuristic" },
  title: "Hosted review",
  change_story: [],
} as unknown as ReviewOverview;

const progress = {
  target_count: 5,
  reviewed: 2,
  changed_since_review: 1,
  needs_changes: 0,
  unreviewed: 2,
  unknown: 0,
  mechanical: 0,
} as ReviewProgress;

function renderHeader(
  authorshipOverride?: { prefix: string; label: string },
  overrides: Partial<React.ComponentProps<typeof ReaderHeader>> = {},
) {
  render(
    <ReaderHeader
      overview={overview}
      progress={progress}
      diffStyle="split"
      diffOverflow="scroll"
      focusMode={false}
      contextOpen={false}
      busy={false}
      readOnly={false}
      revisionAdvanced={false}
      codeTheme="github-dark"
      commentCount={3}
      openCommentCount={1}
      primaryAction={{
        kind: "review_changed",
        label: "Re-review changed area",
        detail: "Changed since your last review",
        disabled: false,
        targetId: "target-1",
      }}
      authorshipOverride={authorshipOverride}
      historical={false}
      onCodeThemeChange={vi.fn()}
      onShowDirectory={vi.fn()}
      onShowComments={vi.fn()}
      onShowAsk={vi.fn()}
      onShowHistory={vi.fn()}
      onPrimaryAction={vi.fn()}
      onDownloadPatch={vi.fn()}
      onFinish={vi.fn()}
      onRefresh={vi.fn()}
      onDiffStyleChange={vi.fn()}
      onToggleDiffOverflow={vi.fn()}
      onToggleContext={vi.fn()}
      onToggleFocus={vi.fn()}
      onShowShortcuts={vi.fn()}
      onShowOverview={vi.fn()}
      {...overrides}
    />,
  );
}

describe("ReaderHeader settings access", () => {
  it.each([
    ["local", "/settings/ai"],
    ["customer_hosted", "/settings"],
    ["managed_dedicated", "/settings"],
    ["managed_shared", "/settings"],
  ] as const)("links to the existing Settings page in %s mode, including read-only reviews", async (mode, href) => {
    renderHeader(undefined, { environment: fallbackReviewEnvironment(mode), readOnly: true, historical: true });
    await userEvent.click(screen.getByRole("button", { name: "Review actions" }));
    expect(screen.getByRole("menuitem", { name: "Settings" })).toHaveAttribute("href", href);
  });
});

describe("ReaderHeader keyboard behavior", () => {
  it("contains product-feedback focus and restores it to the trigger on Escape", async () => {
    renderHeader();
    await userEvent.click(screen.getByLabelText("Review actions"));
    const trigger = screen.getByRole("menuitem", { name: "Send LemonCrow feedback" });
    await userEvent.click(trigger);

    expect(screen.getByRole("dialog", { name: "Make a wish" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Feedback" }));
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Make a wish" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("Review actions")));
  });
});

describe("ReaderHeader wish submission", () => {
  async function openWish() {
    renderHeader();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Send LemonCrow feedback" }));
  }

  it("posts the wish and its optional contact email to the server", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ ok: true, delivered: "remote" }));
    await openWish();
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "Add a jump-to-next-hunk shortcut");
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback contact email" }), "me@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Send wish" }));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledWith(
      "/api/feedback",
      expect.objectContaining({ method: "POST" }),
    ));
    const body = JSON.parse(vi.mocked(authenticatedFetch).mock.calls[0][1]?.body as string);
    expect(body).toEqual({
      message: "Add a jump-to-next-hunk shortcut",
      email: "me@example.com",
      source: "review",
    });
    expect(await screen.findByText(/in the wish backlog/i)).toBeTruthy();
  });

  it("keeps the wish on screen, with a mailto escape hatch, when the server could not deliver it", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("nope", { status: 502 }));
    await openWish();
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "Teach me the map view");
    await userEvent.click(screen.getByRole("button", { name: "Send wish" }));

    expect(await screen.findByText(/couldn.t reach LemonCrow/i)).toBeTruthy();
    expect(screen.queryByText(/wish backlog/i)).toBeNull();
    expect(screen.getByRole("textbox", { name: "Feedback" })).toBeTruthy();
    const fallback = screen.getByRole("link", { name: "email it instead" });
    expect(fallback.getAttribute("href")).toContain("mailto:contact@lemoncrow.com");
    expect(fallback.getAttribute("href")).toContain("Teach%20me%20the%20map%20view");
  });

  it("treats an unexpected delivery verdict as a failure rather than a send", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ ok: true, delivered: "elsewhere" }));
    await openWish();
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "Teach me the map view");
    await userEvent.click(screen.getByRole("button", { name: "Send wish" }));

    expect(await screen.findByText(/couldn.t reach LemonCrow/i)).toBeTruthy();
  });
});

describe("ReaderHeader diff layout preference", () => {
  it("offers Auto, Split, and Unified and sends the explicit user preference", async () => {
    const onDiffStyleChange = vi.fn();
    renderHeader(undefined, { diffStyle: "auto", onDiffStyleChange });

    await userEvent.click(screen.getByLabelText("Review actions"));
    const diffView = screen.getByRole("menuitem", { name: /Diff view/ });
    expect(diffView.textContent).toContain("Auto");
    await userEvent.click(diffView);
    expect(await screen.findByRole("menuitemradio", { name: "Auto · responsive" })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: "Split" })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: "Unified" })).toBeTruthy();

    await userEvent.click(screen.getByRole("menuitemradio", { name: "Unified" }));
    expect(onDiffStyleChange).toHaveBeenCalledWith("unified");
  });
});

describe("ReaderHeader primary action", () => {
  it("uses one feedback control and omits the duplicate next-area action", async () => {
    renderHeader(undefined, {
      primaryAction: { kind: "review_target", label: "Review next area", detail: "Next", disabled: false },
      handoffControls: <UiButton>Send feedback</UiButton>,
    });
    expect(screen.getByRole("button", { name: "Send feedback" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review next area" })).toBeNull();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("menuitem", { name: "Review next area" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Finish review…" })).toBeTruthy();
  });

  it("renders and executes the state-machine action descriptor", async () => {
    const onPrimaryAction = vi.fn();
    renderHeader(undefined, {
      primaryAction: {
        kind: "rereview_comments",
        label: "Re-review 2",
        detail: "The coding agent marked these comments addressed; only your re-review can resolve them.",
        disabled: false,
      },
      onPrimaryAction,
    });

    const action = screen.getByRole("button", { name: "Re-review 2" });
    await userEvent.hover(action);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("only your re-review can resolve them");
    await userEvent.click(action);
    expect(onPrimaryAction).toHaveBeenCalledTimes(1);
  });

  it("shows the active mutation instead of a stale disabled action label", () => {
    renderHeader(undefined, {
      busy: true,
      busyLabel: "Updating review…",
      primaryAction: {
        kind: "review_target",
        label: "Review next area",
        detail: "Continue reviewing the current immutable revision.",
        disabled: false,
        targetId: "t-1",
      },
    });
    const updating = screen.getByRole("button", { name: "Updating review…" });
    // Disabled with a hint: focusable and aria-disabled so the reason stays reachable.
    expect(updating).toHaveAttribute("aria-disabled", "true");
  });

  it("renders non-actionable workflow states as disabled", async () => {
    const onPrimaryAction = vi.fn();
    renderHeader(undefined, {
      primaryAction: {
        kind: "delivery_pending",
        label: "Delivery pending",
        detail: "This handoff is not safe to retry automatically.",
        disabled: true,
      },
      onPrimaryAction,
    });
    const pending = screen.getByRole("button", { name: "Delivery pending" });
    expect(pending).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(pending);
    expect(onPrimaryAction).not.toHaveBeenCalled();
  });

  it("puts an apply-ready review patch in the actions menu", async () => {
    const onDownloadPatch = vi.fn();
    renderHeader(undefined, { onDownloadPatch });

    await userEvent.click(screen.getByLabelText("Review actions"));
    const download = screen.getByRole("menuitem", { name: /Download Snapshot patch/i });
    expect(download.textContent).toContain("git apply");
    await userEvent.click(download);
    expect(onDownloadPatch).toHaveBeenCalledTimes(1);
  });

  it("keeps finish as a secondary explicit action while primary work remains", async () => {
    const onFinish = vi.fn();
    renderHeader(undefined, {
      primaryAction: {
        kind: "review_target",
        label: "Review next area",
        detail: "Needs human judgment",
        disabled: false,
        targetId: "target-1",
      },
      onFinish,
    });

    expect(screen.getByRole("button", { name: "Review next area" })).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
    expect(onFinish).toHaveBeenCalledTimes(1);
  });
});

describe("ReaderHeader review context", () => {
  it("does not present heuristic revision model provenance as a singular author", () => {
    renderHeader();
    expect(screen.queryByText("claude-opus-5")).toBeNull();
    expect(screen.queryByText("agent signal")).toBeNull();
  });

  it("shows an explicit hosted repository context without replacing authorship", () => {
    renderHeader({ prefix: "by", label: "author-a" }, {
      contextOverride: { label: "repo-checkout", title: "Repository repo-checkout" },
      environment: fallbackReviewEnvironment("customer_hosted"),
    });
    expect(screen.getByLabelText("Review context: repo-checkout")).toBeTruthy();
    expect(screen.getByText("author-a")).toBeTruthy();
  });
});

describe("ReaderHeader extension authorship", () => {
  it("uses an explicit outer-composition authorship label", () => {
    renderHeader({ prefix: "by", label: "alice@example.com" });
    expect(screen.getByText("alice@example.com")).toBeTruthy();
  });
});


describe("ReaderHeader environment context", () => {
  it("names the local source and keeps local refresh available", async () => {
    const localOverview = {
      ...overview,
      session: { ...overview.session, actor_type: "human", range_mode: "working_tree", source_ref: "", repo_root: "local:repo_0123" },
      workspace_root: "/workspace/repo",
    } as ReviewOverview;
    renderHeader(undefined, {
      overview: localOverview,
      environment: fallbackReviewEnvironment("local"),
      allowRefresh: true,
    });

    expect(screen.queryByText("Working tree")).toBeNull();
    expect(screen.getByRole("button", { name: /Snapshot 4; open Snapshot history/ })).toBeTruthy();
    expect(screen.getByLabelText("Review environment: Local")).toBeTruthy();
    expect(screen.getByLabelText("Review workspace: /workspace/repo")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.getByRole("menuitem", { name: "Update from current source" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: /Copy workspace path/ }).textContent).toContain("/workspace/repo");
  });

  it("never offers an opaque server repository identity as a workspace path", async () => {
    const localOverview = {
      ...overview,
      session: { ...overview.session, actor_type: "human", range_mode: "working_tree", source_ref: "", repo_root: "local:repo_0123" },
      workspace_root: "",
    } as ReviewOverview;
    renderHeader(undefined, { overview: localOverview, environment: fallbackReviewEnvironment("local") });
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("menuitem", { name: /Copy workspace path/ })).toBeNull();
  });

  it("does not expose local refresh in hosted mode", async () => {
    renderHeader(undefined, {
      environment: fallbackReviewEnvironment("customer_hosted"),
      allowRefresh: false,
    });

    expect(screen.getByLabelText("Review environment: Customer hosted")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("menuitem", { name: "Update from current source" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /Copy workspace path/ })).toBeNull();
  });

  it("does not expose editor-launch actions in the review overflow menu", async () => {
    renderHeader(undefined, { environment: fallbackReviewEnvironment("local") });

    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("button", { name: /Open in / })).toBeNull();
  });

  it("does not expose editor-launch actions in hosted mode", async () => {
    renderHeader(undefined, { environment: fallbackReviewEnvironment("customer_hosted") });
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("button", { name: /Open in / })).toBeNull();
  });
});

describe("ReaderHeader chrome", () => {
  it("never exposes Tour as a persistent category and hides unearned Map", () => {
    renderHeader(undefined, {
      view: "tour",
      availableViews: ["brief", "implementation"],
      onViewChange: vi.fn(),
    });
    const workspace = screen.getByRole("navigation", { name: "Review workspace" });
    expect(within(workspace).getByRole("button", { name: "Brief" })).toHaveAttribute("aria-pressed", "true");
    expect(within(workspace).getByRole("button", { name: "Code" })).toBeTruthy();
    expect(within(workspace).queryByRole("button", { name: "Tour" })).toBeNull();
    expect(within(workspace).queryByRole("button", { name: "Map" })).toBeNull();
  });

  it("keeps Activity, status and the primary action in one header row", async () => {
    const onShowDevelopment = vi.fn();
    const onShowReadiness = vi.fn();
    renderHeader(undefined, {
      development: {
        contributors: 3,
        newerEvents: 4,
        newerFiles: 2,
        active: true,
        label: "Activity: 3 contributors in Snapshot 4 · agents working now · 2 newer files",
      },
      onShowDevelopment,
      onViewChange: vi.fn(),
      readiness: { label: "Ready to merge", detail: "All visible merge requirements are satisfied.", text: "Ready", className: "text-emerald-300" },
      onShowReadiness,
    });

    const header = screen.getByRole("banner");
    const workspace = within(header).getByRole("navigation", { name: "Review workspace" });
    expect(Array.from(workspace.querySelectorAll("button"), (button) => button.getAttribute("aria-label"))).toEqual([
      "Brief",
      "Code",
      "Map",
      "Activity: 3 contributors in Snapshot 4 · agents working now · 2 newer files",
      "Review comments: 1 open, 3 total",
      "Review Status: Ready to merge",
      "Ask about this review",
    ]);
    expect(within(header).queryByText("2 / 5 reviewed")).toBeNull();
    const status = within(header).getByRole("button", { name: "Review Status: Ready to merge" });
    expect(status).toHaveTextContent("Ready");
    const activity = within(header).getByRole("button", { name: /Activity: 3 contributors in Snapshot 4/ });
    expect(activity.textContent).toContain("3");
    expect(activity.querySelector(".review-live-dot")?.getAttribute("data-active")).toBe("true");
    await userEvent.click(activity);
    expect(onShowDevelopment).toHaveBeenCalledTimes(1);

    await userEvent.click(status);
    expect(onShowReadiness).toHaveBeenCalledTimes(1);
    expect(within(header).getByRole("button", { name: "Re-review changed area" })).toBeTruthy();
  });

  it("omits the Activity dot when nothing happened after the Snapshot", () => {
    renderHeader(undefined, {
      development: { contributors: 1, newerEvents: 0, newerFiles: 0, active: false, label: "Activity: 1 contributor in Snapshot 4" },
      onShowDevelopment: vi.fn(),
    });
    const activity = screen.getByRole("button", { name: "Activity: 1 contributor in Snapshot 4" });
    expect(activity.querySelector(".review-live-dot")).toBeNull();
  });
  it("omits Ask when the Review composition does not provide it", () => {
    renderHeader(undefined, { onShowAsk: undefined });
    expect(screen.queryByRole("button", { name: "Ask about this review" })).not.toBeInTheDocument();
  });

  it("phone composition: row 2 is views only; primary action and tools live in the bottom dock", async () => {
    const dock = document.createElement("div");
    dock.dataset.testid = "dock";
    document.body.appendChild(dock);
    const onShowReadiness = vi.fn();
    try {
      renderHeader(undefined, {
        onViewChange: vi.fn(),
        availableViews: ["brief", "implementation"],
        readiness: { label: "Review in progress", detail: "3 left", text: "2/5 reviewed · Readiness", shortText: "Readiness", className: "" },
        onShowReadiness,
        uncertainSignals: 2,
        compactDock: { target: dock, navigation: <UiButton type="button">Changes · 3</UiButton> },
      });
      const header = screen.getByRole("banner");
      const workspace = within(header).getByRole("navigation", { name: "Review workspace" });
      expect(Array.from(workspace.querySelectorAll("button"), (button) => button.getAttribute("aria-label"))).toEqual(["Brief", "Code"]);
      expect(within(header).queryByRole("button", { name: "Re-review changed area" })).toBeNull();
      expect(within(header).getByRole("button", { name: "Review actions" })).toBeTruthy();

      const primary = within(dock).getByRole("button", { name: "Re-review changed area" });
      const tools = within(dock).getByRole("group", { name: "Review tools" });
      // Primary first, then the labelled tools; nothing in the header row needs scrolling to find.
      expect(primary.compareDocumentPosition(tools) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(Array.from(tools.querySelectorAll("button"), (button) => button.getAttribute("aria-label") ?? button.textContent)).toEqual([
        "Changes · 3",
        "Review comments: 1 open, 3 total",
        "Review Status: Review in progress · 2 uncertain signals",
        "Ask about this review",
      ]);
      const status = within(tools).getByRole("button", { name: /Review Status/ });
      expect(status).toHaveTextContent("Readiness");
      expect(status).not.toHaveTextContent("2/5 reviewed");
      for (const label of tools.querySelectorAll(".review-tool-label")) expect(label.textContent).not.toBe("");
      await userEvent.click(status);
      expect(onShowReadiness).toHaveBeenCalledTimes(1);
    } finally {
      dock.remove();
    }
  });

  it("desktop Status carries progress and uncertainty: no second status readout", () => {
    renderHeader(undefined, {
      readiness: { label: "Review in progress", detail: "3 left", text: "2/5 reviewed · Readiness", shortText: "Readiness", className: "" },
      onShowReadiness: vi.fn(),
      uncertainSignals: 1,
    });
    const status = screen.getByRole("button", { name: "Review Status: Review in progress · 1 uncertain signal" });
    expect(status).toHaveTextContent("2/5 reviewed · Readiness· 1 uncertain");
    expect(document.querySelectorAll("[data-review-status-authority]")).toHaveLength(1);
  });

  it("UX gate: every header control that can render is registered, with at most three views", () => {
    const fullOverview = {
      ...overview,
      change: { work_items: [{ external_id: "ENG-1", title: "Ticket", url: "https://example.invalid/ENG-1" }], relations: [] },
    } as unknown as ReviewOverview;
    for (const handoff of [false, true]) {
      const { unmount } = render(
        <ReaderHeader
          overview={fullOverview}
          progress={progress}
          diffStyle="split"
          diffOverflow="scroll"
          focusMode={false}
          contextOpen={false}
          busy={false}
          readOnly={false}
          revisionAdvanced={false}
          codeTheme="github-dark"
          commentCount={3}
          openCommentCount={1}
          primaryAction={{ kind: "review_target", label: "Review next", detail: "", disabled: false, targetId: "t" }}
          historical={false}
          view="implementation"
          availableViews={["brief", "implementation", "map"]}
          development={{ contributors: 2, newerEvents: 1, newerFiles: 0, active: true, label: "Activity: 2 contributors" }}
          onShowDevelopment={vi.fn()}
          readiness={{ label: "Ready", detail: "", text: "Ready", className: "" }}
          onShowReadiness={vi.fn()}
          snapshotControls={<ReviewSnapshotControl files={2} detail="moved" compareHref="/x" busy={false} onCapture={vi.fn()} onHistory={vi.fn()} />}
          handoffControls={handoff ? (
            <ReviewHandoffControls reviewId="r" delivery={null} lastDelivery={null} unsent={2} waiting={false} busy={false} onSendNow={vi.fn()} onShowHandoff={vi.fn()} onDestinationChanged={vi.fn()} />
          ) : undefined}
          backtrail={{ label: "Brief", onBack: vi.fn() }}
          onCodeThemeChange={vi.fn()}
          onShowDirectory={vi.fn()}
          onShowComments={vi.fn()}
          onShowAsk={vi.fn()}
          onViewChange={vi.fn()}
          onShowHistory={vi.fn()}
          onPrimaryAction={vi.fn()}
          onDownloadPatch={vi.fn()}
          onFinish={vi.fn()}
          onRefresh={vi.fn()}
          onDiffStyleChange={vi.fn()}
          onToggleDiffOverflow={vi.fn()}
          onToggleContext={vi.fn()}
          onToggleFocus={vi.fn()}
          onShowShortcuts={vi.fn()}
          onShowOverview={vi.fn()}
        />,
      );
      const toolbar = screen.getByRole("banner").querySelector(".review-reader-toolbar")!;
      const controls = Array.from(toolbar.querySelectorAll<HTMLElement>("button, a[href], [role='button']"));
      const unregistered = controls
        .map((control) => ({
          name: control.getAttribute("aria-label") ?? control.textContent?.trim() ?? "",
          primary: control.classList.contains("review-reader-primary"),
        }))
        .filter((control) => !registeredHeaderControl(control.name, { primary: control.primary }));
      expect(unregistered).toEqual([]);
      const views = within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button");
      expect(views.length).toBeLessThanOrEqual(REVIEW_MAX_PERSISTENT_VIEWS);
      expect(toolbar.querySelectorAll(".review-reader-primary")).toHaveLength(1);
      unmount();
    }
  });

  it("offers Snapshot history from More, since the phone header hides the Snapshot chip", async () => {
    const onShowHistory = vi.fn();
    renderHeader(undefined, { onShowHistory });
    await userEvent.click(screen.getByRole("button", { name: "Review actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Snapshot history" }));
    expect(onShowHistory).toHaveBeenCalledTimes(1);
  });
});
