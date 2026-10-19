import { createRef } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewOutline from "./ReviewOutline";
import { fileAttention, reviewAttentionPath } from "./readerModel";
import type { ReviewOutlineItem, ReviewTarget, ReviewTargetPriority } from "./types";

function outlineTarget(id: string, path: string, priority: ReviewTargetPriority): ReviewTarget {
  return {
    target_id: id,
    unit_key: id,
    kind: "symbol",
    path,
    label: `${path}::${id}`,
    symbol: id,
    start_line: 1,
    end_line: 1,
    hunk_ordinals: [0],
    spans: [],
    state: priority.outstanding ? "unreviewed" : "reviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: ["public contract changed"],
    additions: 1,
    deletions: 0,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    priority,
  };
}

const ROWS: ReviewOutlineItem[] = [
  {
    path: "src/a.py",
    target_count: 2,
    reviewed: 0,
    changed_since_review: 1,
    needs_changes: 0,
    unreviewed: 1,
    unknown: 0,
    mechanical: 0,
    attention_rank: 1,
    reasons: ["public contract changed"],
    section: "review_first",
    position: 0,
    next_target_id: "target-a",
    next_reason: "Changed since your review",
    next_reason_code: "changed_since_judgment",
  },
  {
    path: "src/b.py",
    target_count: 1,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 1,
    unknown: 0,
    mechanical: 0,
    attention_rank: 2,
    reasons: ["+1 -0"],
    section: "unreviewed",
    position: 1,
  },
];

describe("ReviewOutline accessibility", () => {
  it("puts a failing required check above the next review item", async () => {
    const onReviewAction = vi.fn();
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        reviewActions={[{ id: "q:required:browser:checkout", title: "Required check failing", detail: "browser:checkout failed on Snapshot 3." }]}
        onReviewAction={onReviewAction}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );
    const action = screen.getByRole("button", { name: /Required check failing/ });
    expect(action.textContent).toContain("browser:checkout failed on Snapshot 3.");
    await userEvent.click(action);
    expect(onReviewAction).toHaveBeenCalledWith("q:required:browser:checkout");
  });

  it("picks the order beside search and lists every file by path in File order", async () => {
    const onOrderChange = vi.fn();
    const props = {
      activePath: "src/a.py",
      query: "",
      searchInputRef: createRef<HTMLInputElement>(),
      matchTargetCount: 0,
      matchFileCount: 0,
      matchCountsByPath: new Map<string, number>(),
      onQuery: vi.fn(),
      onNavigateMatch: vi.fn(),
      onDismissSearch: vi.fn(),
      onSelectPath: vi.fn(),
      collapsed: false,
      onToggleCollapsed: vi.fn(),
      onOrderChange,
    };
    const rows = [ROWS[1], ROWS[0]];
    const { rerender } = render(<ReviewOutline {...props} rows={rows} order="recommended" />);

    await userEvent.click(screen.getByRole("button", { name: "Review order: Recommended" }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /File order/ }));
    expect(onOrderChange).toHaveBeenCalledWith("file");

    rerender(<ReviewOutline {...props} rows={rows} order="file" />);
    expect(screen.getByRole("button", { name: "Review order: File order" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Review first/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Unreviewed/i })).toBeNull();
    const files = [...document.querySelectorAll(".review-outline-file")].map((node) => node.getAttribute("title") ?? node.textContent);
    expect(files.map((text) => (text?.includes("a.py") ? "a" : "b"))).toEqual(["a", "b"]);
  });

  it("exposes section expansion and the active file semantically", async () => {
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getByRole("complementary", { name: "Review outline" })).toBeTruthy();
    const changed = screen.getByRole("button", { name: /Review first/i });
    expect(changed.getAttribute("aria-expanded")).toBe("true");
    const activeFile = screen.getByRole("button", { name: /a.py/i });
    expect(activeFile.getAttribute("aria-current")).toBe("location");
    expect(activeFile.querySelector('[data-file-icon="a.py"]')).toBeTruthy();
    expect(screen.getByText("a.py").getAttribute("title")).toBe("src/a.py");

    await userEvent.click(changed);
    expect(changed.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: /a.py/i })).toBeNull();
  });

  it("groups files under compact parent-directory labels", () => {
    const rows: ReviewOutlineItem[] = [
      {
        ...ROWS[0],
        path: ".gitignore",
        changed_since_review: 0,
        reasons: ["+1 -0"],
        section: "unreviewed",
      },
      {
        ...ROWS[0],
        path: "src/components/ReviewSummaryCard.tsx",
        changed_since_review: 0,
        section: "unreviewed",
        reasons: ["+23 -20"],
      },
      {
        ...ROWS[1],
        path: "src/components/Button.tsx",
        reasons: ["+4 -1"],
      },
      {
        ...ROWS[1],
        path: "src/lib/reviewCopy.ts",
        reasons: ["+7 -0"],
      },
    ];

    render(
      <ReviewOutline
        rows={rows}
        activePath=""
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    const directories = screen.getAllByTestId("review-outline-directory").map((node) => node.textContent);
    expect(directories).toEqual(["src/components/", "src/lib/"]);
    expect(screen.getAllByText("src/components/")).toHaveLength(1);
    expect(screen.getByText("ReviewSummaryCard.tsx")).toBeTruthy();
    expect(screen.getByText("Button.tsx")).toBeTruthy();
  });


  it("shows one canonical current-cohort breadcrumb and the live review-first jump", async () => {
    const onSelectTarget = vi.fn();
    const onNavigateAttention = vi.fn();
    const onOpenCohort = vi.fn();
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        currentCohort={{ id: "cohort:auth", label: "Authentication", path: "src/a.py" }}
        nextAttention={{
          targetId: "target-b",
          path: "src/b.py",
          label: "authorize",
          reason: "Changed since your review",
          reasonCode: "changed_since_judgment",
          attention: "review_first",
        }}
        attentionCount={4}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={onSelectTarget}
        onNavigateAttention={onNavigateAttention}
        onOpenCohort={onOpenCohort}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    const guide = screen.getByTestId("review-guide");
    expect(guide).toBeTruthy();
    expect(within(guide).getByText("Current concern")).toBeTruthy();
    expect(screen.queryByText("Change story")).toBeNull();
    expect(screen.getByText("Files")).toBeTruthy();
    expect(within(guide).getByText("Review first")).toBeTruthy();
    const cohort = screen.getByRole("button", { name: "Current concern Authentication" });
    expect(cohort).toHaveAttribute("data-cohort-id", "cohort:auth");
    await userEvent.click(cohort);
    expect(onOpenCohort).toHaveBeenCalledWith("cohort:auth");

    const next = screen.getByRole("button", { name: "Review first: authorize" });
    await userEvent.hover(next);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("src/b.py · authorize · Changed since your review");
    await userEvent.unhover(next);
    await userEvent.click(next);
    expect(onSelectTarget).toHaveBeenCalledWith("target-b");

    await userEvent.click(screen.getByRole("button", { name: "Next review item" }));
    await userEvent.click(screen.getByRole("button", { name: "Previous review item" }));
    expect(onNavigateAttention).toHaveBeenNthCalledWith(1, 1);
    expect(onNavigateAttention).toHaveBeenNthCalledWith(2, -1);
  });

  it("names the Next item's directory without hovering and labels ordinary work Next", () => {
    const rows: ReviewOutlineItem[] = [
      { ...ROWS[1], path: "src/review/store.py", section: "unreviewed", position: 0 },
      { ...ROWS[1], path: "src/legacy/store.py", section: "unreviewed", position: 1 },
    ];
    const { rerender } = render(
      <ReviewOutline
        rows={rows}
        activePath=""
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        nextAttention={{
          targetId: "t",
          path: "src/legacy/store.py",
          label: "prune",
          reason: "Changed since your review",
          reasonCode: "changed_since_judgment",
          attention: "review_first",
        }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );
    const guide = screen.getByTestId("review-guide");
    expect(guide.textContent).toContain("src/legacy/");
    expect(guide.textContent).toContain("store.py");
    expect(guide.textContent).toContain("Review first");
    expect(screen.queryByRole("button", { name: /^Remaining/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Tests/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Unreviewed/ })).toBeTruthy();

    rerender(
      <ReviewOutline
        rows={rows}
        activePath=""
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        nextAttention={{ targetId: "t", path: "src/review/store.py", label: "save", reason: "Not reviewed yet", reasonCode: "unreviewed", attention: "understand" }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );
    expect(screen.getByTestId("review-guide").textContent).toContain("Next");
    expect(screen.getByTestId("review-guide").textContent).not.toContain("Review first");
    expect(screen.getByRole("button", { name: "Next: save" })).toBeTruthy();
  });

  it("renders files in canonical order even when directories interleave", () => {
    const rows: ReviewOutlineItem[] = [
      { ...ROWS[1], path: "src/a/x.py", section: "unreviewed", position: 0 },
      { ...ROWS[1], path: "lib/y.py", section: "unreviewed", position: 1 },
      { ...ROWS[1], path: "src/a/z.py", section: "unreviewed", position: 2 },
    ];
    render(
      <ReviewOutline
        rows={rows}
        activePath=""
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );
    const files = Array.from(document.querySelectorAll(".review-outline-file")).map((node) => node.getAttribute("title")
      ?? node.querySelector("[title]")?.getAttribute("title"));
    expect(files).toEqual(["src/a/x.py", "lib/y.py", "src/a/z.py"]);
    expect(screen.getAllByTestId("review-outline-directory").map((node) => node.textContent)).toEqual([
      "src/a/",
      "lib/",
      "src/a/",
    ]);
  });

  it("keeps a large virtualized outline populated after a far scroll and list shrink", () => {
    const rows = Array.from({ length: 500 }, (_, index): ReviewOutlineItem => ({
      ...ROWS[1],
      path: `src/large/file-${String(index).padStart(4, "0")}.ts`,
      position: index,
    }));
    const props = {
      activePath: "",
      query: "",
      searchInputRef: createRef<HTMLInputElement>(),
      matchTargetCount: 0,
      matchFileCount: 0,
      matchCountsByPath: new Map<string, number>(),
      onQuery: vi.fn(),
      onNavigateMatch: vi.fn(),
      onDismissSearch: vi.fn(),
      onSelectPath: vi.fn(),
      collapsed: false,
      onToggleCollapsed: vi.fn(),
      order: "file" as const,
    };
    const { rerender } = render(<ReviewOutline {...props} rows={rows} />);
    const virtual = screen.getByTestId("review-outline-virtualized");
    Object.defineProperty(virtual, "scrollTop", { value: 16_000, writable: true, configurable: true });
    fireEvent.scroll(virtual);

    rerender(<ReviewOutline {...props} rows={rows.slice(0, 401)} />);

    const visible = Array.from(document.querySelectorAll(".review-outline-file"));
    expect(visible.length).toBeGreaterThan(0);
    expect(visible.some((node) => node.getAttribute("data-review-path") === "src/large/file-0400.ts")).toBe(true);
    const row = visible[0] as HTMLElement;
    expect(row.style.height).toBe("34px");
    expect(row.style.marginBottom).toBe("2px");
  });

  it("omits orientation when a small review would only repeat the file list", () => {
    render(
      <ReviewOutline
        rows={ROWS.slice(0, 1)}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        nextAttention={{
          targetId: "target-a",
          path: "src/a.py",
          label: "authorize",
          reason: "Not reviewed yet",
          reasonCode: "unreviewed",
          attention: "understand",
        }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("review-guide")).toBeNull();
  });

  it("hides orientation while an explicit search is active", () => {
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query="contract"
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={1}
        matchFileCount={1}
        matchCountsByPath={new Map([["src/a.py", 1]])}
        currentCohort={{ id: "identity", label: "Identity", path: "src/a.py" }}
        nextAttention={{
          targetId: "target-a",
          path: "src/a.py",
          label: "authorize",
          reason: "Not reviewed yet",
          reasonCode: "unreviewed",
          attention: "understand",
        }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("review-guide")).toBeNull();
  });

  it("shows target/file match counts and uses Enter, Shift+Enter, and Escape for search flow", async () => {
    const onNavigateMatch = vi.fn();
    const onDismissSearch = vi.fn();
    render(
      <ReviewOutline
        rows={ROWS.slice(0, 1)}
        activePath="src/a.py"
        query="contract"
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={2}
        matchFileCount={1}
        matchCountsByPath={new Map([["src/a.py", 2]])}
        onQuery={vi.fn()}
        onNavigateMatch={onNavigateMatch}
        onDismissSearch={onDismissSearch}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getByText("2 review areas · 1 file")).toBeTruthy();
    expect(screen.getByText("2 matches")).toBeTruthy();
    const input = screen.getByRole("textbox", { name: "Search review" });
    expect(input.classList.contains("review-outline-search-field")).toBe(true);
    await userEvent.click(input);
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Shift>}{Enter}{/Shift}");
    await userEvent.keyboard("{Escape}");

    expect(onNavigateMatch).toHaveBeenNthCalledWith(1, 1);
    expect(onNavigateMatch).toHaveBeenNthCalledWith(2, -1);
    expect(onDismissSearch).toHaveBeenCalledTimes(1);
  });
  it("reads each file's section and reason from the same classification as the Next card", () => {
    const priority = (position: number, tier: number, attention: "review_first" | "understand" | "done", code: string, detail: string) => ({
      version: 1, tier, attention, outstanding: attention !== "done", position, reasons: [{ code, detail }],
    });
    const targets: ReviewTarget[] = [
      outlineTarget("t:plain", "src/store.py", priority(2, 4, "understand", "unreviewed", "Not reviewed yet")),
      outlineTarget("t:conflict", "src/store.py", priority(1, 3, "review_first", "author_guidance_conflict", "Authors disagree about retention")),
      outlineTarget("t:asked", "src/api.py", priority(0, 1, "review_first", "request_change_open", "Requested change is still open")),
      outlineTarget("t:done", "src/done.py", priority(3, 6, "done", "reviewed", "Reviewed; content unchanged")),
    ];
    // Server rows as they read while marks are in flight: stale sections and
    // reasons, plus a ranker descriptor on the finished file.
    const rows: ReviewOutlineItem[] = [
      { ...ROWS[1], path: "src/api.py", section: "unreviewed", position: 0, next_reason: "Not reviewed yet" },
      { ...ROWS[1], path: "src/store.py", section: "unreviewed", position: 1, next_reason: "Not reviewed yet" },
      { ...ROWS[1], path: "src/done.py", section: "unreviewed", position: 3, reasons: ["public contract changed"], reviewed: 1, unreviewed: 0 },
    ];
    const queue = reviewAttentionPath(targets);
    render(
      <ReviewOutline
        rows={rows}
        attentionByPath={fileAttention(targets)}
        activePath="src/done.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        nextAttention={queue[0]}
        attentionCount={queue.length}
        attentionPosition={1}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    const reviewFirst = document.getElementById("review-outline-section-review_first");
    expect(reviewFirst).toBeTruthy();
    const api = within(reviewFirst!).getByRole("button", { name: /api\.py/ });
    const store = within(reviewFirst!).getByRole("button", { name: /store\.py/ });
    expect(api.textContent).toContain("Requested change is still open");
    expect(store.textContent).toContain("Authors disagree about retention");
    expect(store.textContent).not.toContain("Not reviewed yet");
    // The Next card names the same item and reason as the first row.
    expect(screen.getByRole("button", { name: "Review first: t:asked" }).textContent).toContain("Requested change is still open");
    // A finished file is Done and shows no reason, even while active.
    expect(screen.getByRole("button", { name: /^Done/ })).toBeTruthy();
    expect(screen.queryByText("public contract changed")).toBeNull();
  });

  it("groups files under compact parent-directory labels", () => {
    const groupedRows: ReviewOutlineItem[] = [
      { ...ROWS[1], path: "AGENTS.md" },
      { ...ROWS[1], path: "frontend/src/App.tsx" },
      { ...ROWS[1], path: "frontend/src/main.tsx" },
      { ...ROWS[1], path: "frontend/scripts/check-ui.ts" },
    ];

    render(
      <ReviewOutline
        rows={groupedRows}
        activePath="frontend/src/App.tsx"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getAllByTestId("review-outline-directory").map((node) => node.textContent)).toEqual([
      "frontend/src/",
      "frontend/scripts/",
    ]);
    expect(screen.getByText("App.tsx").getAttribute("title")).toBe("frontend/src/App.tsx");
    expect(screen.getByText("main.tsx").getAttribute("title")).toBe("frontend/src/main.tsx");
  });

  it("virtualizes a large outline while preserving cross-page file navigation", async () => {
    const rows = Array.from({ length: 1_000 }, (_, index): ReviewOutlineItem => ({
      ...ROWS[1],
      path: `src/generated/file-${String(index).padStart(4, "0")}.ts`,
      position: index,
    }));
    const pageByPath = new Map(rows.map((row, index) => [row.path, Math.floor(index / 200) + 1] as const));

    render(
      <ReviewOutline
        rows={rows}
        pageByPath={pageByPath}
        activePath={rows[999].path}
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    const virtual = screen.getByTestId("review-outline-virtualized");
    expect(screen.getByText("1000")).toBeTruthy();
    const rendered = virtual.querySelectorAll(".review-outline-file");
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(100);
    expect([...rendered].some((node) => node.getAttribute("data-review-page") === "5")).toBe(true);
    expect(virtual.querySelector('[data-review-path="src/generated/file-0999.ts"]')).toBeTruthy();
  });

  it.each([400, 401])("keeps every file reachable at the virtualization boundary (%i)", (count) => {
    const rows = Array.from({ length: count }, (_, index) => ({ ...ROWS[1], path: `src/file-${index}.ts`, position: index }));
    const onSelectPath = vi.fn();
    render(<ReviewOutline
      rows={rows} activePath={rows[count - 1].path} query="" order="file"
      searchInputRef={createRef<HTMLInputElement>()} matchTargetCount={0} matchFileCount={0} matchCountsByPath={new Map()}
      onQuery={vi.fn()} onNavigateMatch={vi.fn()} onDismissSearch={vi.fn()} onSelectPath={onSelectPath}
      collapsed={false} onToggleCollapsed={vi.fn()}
    />);
    const virtual = screen.queryByTestId("review-outline-virtualized");
    expect(Boolean(virtual)).toBe(count > 400);
    expect(document.querySelector(`[data-review-path="${rows[count - 1].path}"]`)).toBeTruthy();
    const mounted = document.querySelectorAll(".review-outline-file");
    expect(mounted.length).toBe(count === 400 ? count : Math.min(count, 38));
    fireEvent.click(document.querySelector(`[data-review-path="${rows[count - 1].path}"]`)!);
    expect(onSelectPath).toHaveBeenCalledWith(rows[count - 1].path);
  });

  it("keeps the farthest file reachable after scrolling and a large list shrink", () => {
    const rows = Array.from({ length: 10_000 }, (_, index) => ({ ...ROWS[1], path: `src/file-${String(index).padStart(5, "0")}.ts`, position: index }));
    const onSelectPath = vi.fn();
    const view = (next: typeof rows) => <ReviewOutline
      rows={next} activePath="" query="" order="file" searchInputRef={createRef<HTMLInputElement>()}
      matchTargetCount={0} matchFileCount={0} matchCountsByPath={new Map()} onQuery={vi.fn()}
      onNavigateMatch={vi.fn()} onDismissSearch={vi.fn()} onSelectPath={onSelectPath}
      collapsed={false} onToggleCollapsed={vi.fn()}
    />;
    const { rerender } = render(view(rows));
    const virtual = screen.getByTestId("review-outline-virtualized");
    fireEvent.scroll(virtual, { target: { scrollTop: 360_000 } });
    const last = virtual.querySelector(`[data-review-path="${rows[9_999].path}"]`)!;
    expect(last).toBeTruthy();
    expect(last.classList.contains("mb-0.5")).toBe(false);
    fireEvent.click(last);
    expect(onSelectPath).toHaveBeenLastCalledWith(rows[9_999].path);
    rerender(view(rows.slice(0, 401)));
    expect(virtual.querySelector(`[data-review-path="${rows[400].path}"]`)).toBeTruthy();
    expect(virtual.querySelectorAll(".review-outline-file").length).toBeLessThan(100);
  });

  it("supports keyboard traversal across windows and keeps filtered far-file navigation", async () => {
    const rows = Array.from({ length: 1_000 }, (_, index) => ({ ...ROWS[1], path: `src/file-${String(index).padStart(4, "0")}.ts`, position: index }));
    const onSelectPath = vi.fn();
    const props = {
      activePath: "", query: "", order: "file" as const, searchInputRef: createRef<HTMLInputElement>(),
      matchTargetCount: 0, matchFileCount: 0, matchCountsByPath: new Map<string, number>(),
      onQuery: vi.fn(), onNavigateMatch: vi.fn(), onDismissSearch: vi.fn(), onSelectPath,
      collapsed: false, onToggleCollapsed: vi.fn(),
    };
    const { rerender } = render(<ReviewOutline rows={rows} {...props} />);
    const virtual = screen.getByRole("group", { name: "Changed files" });
    virtual.focus();
    await userEvent.keyboard("{End}");
    expect(document.activeElement?.getAttribute("data-review-path")).toBe(rows[999].path);
    await userEvent.keyboard("{ArrowUp}{Enter}");
    expect(onSelectPath).toHaveBeenLastCalledWith(rows[998].path);
    await userEvent.keyboard("{Home}");
    expect(document.activeElement?.getAttribute("data-review-path")).toBe(rows[0].path);
    await userEvent.keyboard("{PageDown}");
    expect(document.activeElement?.getAttribute("data-review-path")).toBe(rows[13].path);
    rerender(<ReviewOutline rows={[rows[999]]} {...props} query="file-0999" matchFileCount={1} matchTargetCount={1} />);
    await userEvent.click(screen.getByRole("button", { name: /file-0999/ }));
    expect(onSelectPath).toHaveBeenLastCalledWith(rows[999].path);
  });

  it("virtualizes a large outline while preserving cross-page file navigation", async () => {
    const rows = Array.from({ length: 1_000 }, (_, index): ReviewOutlineItem => ({
      ...ROWS[1],
      path: `src/generated/file-${String(index).padStart(4, "0")}.ts`,
      position: index,
    }));
    const pageByPath = new Map(rows.map((row, index) => [row.path, Math.floor(index / 200) + 1] as const));

    render(
      <ReviewOutline
        rows={rows}
        pageByPath={pageByPath}
        activePath={rows[999].path}
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    const virtual = screen.getByTestId("review-outline-virtualized");
    expect(screen.getByText("1000")).toBeTruthy();
    const rendered = virtual.querySelectorAll(".review-outline-file");
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(100);
    expect([...rendered].some((node) => node.getAttribute("data-review-page") === "5")).toBe(true);
    expect(virtual.querySelector('[data-review-path="src/generated/file-0999.ts"]')).toBeTruthy();
  });

});
