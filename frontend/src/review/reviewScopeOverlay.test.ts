import { afterEach, describe, expect, it } from "vitest";

import { decorateReviewTargetFolds, isFoldHiddenRow, renderedDiffLine } from "./reviewScopeOverlay";
import type { ReviewTarget } from "./types";

function row(line: number, type: string): string {
  return `<div data-line="${line}" data-line-type="${type}"></div>`;
}

/** Pierre's split layout: a deletions column, then an additions column, each numbered on its own side. */
function splitDiff(deletions: string, additions: string): HTMLElement {
  const item = document.createElement("div");
  const host = document.createElement("diffs-container");
  item.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<pre><code data-code data-deletions>${deletions}</code><code data-code data-additions>${additions}</code></pre>`;
  document.body.appendChild(item);
  return item;
}

describe("renderedDiffLine", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("finds a split diff's new line in the additions column, not the old context line with the same number", () => {
    // Three lines were added above, so old line 61 (context) sits beside new line 64,
    // and new line 61 (the change) sits further up in the additions column.
    const item = splitDiff(row(60, "context") + row(61, "context"), row(61, "change-addition") + row(64, "context"));

    const line = renderedDiffLine(item, "additions", 61);

    expect(line?.closest("[data-code]")?.hasAttribute("data-additions")).toBe(true);
    expect(line?.getAttribute("data-line-type")).toBe("change-addition");
  });

  it("finds a split diff's old line in the deletions column", () => {
    const item = splitDiff(row(59, "change-deletion"), row(59, "context"));

    const line = renderedDiffLine(item, "deletions", 59);

    expect(line?.closest("[data-code]")?.hasAttribute("data-deletions")).toBe(true);
  });

  it("keeps using the line type in a unified diff", () => {
    const item = document.createElement("div");
    const host = document.createElement("diffs-container");
    item.appendChild(host);
    host.attachShadow({ mode: "open" }).innerHTML = `<pre><code data-code data-unified>${row(7, "change-deletion")}${row(7, "change-addition")}</code></pre>`;
    document.body.appendChild(item);

    expect(renderedDiffLine(item, "additions", 7)?.getAttribute("data-line-type")).toBe("change-addition");
    expect(renderedDiffLine(item, "deletions", 7)?.getAttribute("data-line-type")).toBe("change-deletion");
  });
});

function hunk(id: string, spans: ReviewTarget["spans"]): ReviewTarget {
  const lines = spans.flatMap((span) => [span.start_line, span.end_line]);
  return {
    target_id: id,
    unit_key: id,
    kind: "hunk",
    path: "src/app.py",
    label: id,
    symbol: "",
    start_line: Math.min(...lines),
    end_line: Math.max(...lines),
    hunk_ordinals: [0],
    spans,
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 1,
    fingerprint_method: "",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

/**
 * Pierre's real split grid: each column is a gutter and a content part whose
 * children fill the same grid rows, a side with no line holding a buffer.
 *
 *   row  deletions      additions
 *   0    ctx 1 (new 1)  ctx 1 (old 1)
 *   1    del 2          add 2
 *   2    del 3          buffer x2
 *   3    del 4          (buffer)
 *   4    ctx 5 (new 3)  ctx 3 (old 5)
 *   5    ctx 6 (new 4)  ctx 4 (old 6)
 */
function splitGrid(): { item: HTMLElement; root: ShadowRoot } {
  const content = (line: number, type: string, alt?: number) => (
    `<div data-line="${line}"${alt ? ` data-alt-line="${alt}"` : ""} data-line-type="${type}"></div>`
  );
  const gutter = (line: number) => `<div data-column-number="${line}"></div>`;
  const contentBuffer = (size: number) => `<div data-content-buffer data-buffer-size="${size}" style="grid-row: span ${size};"></div>`;
  const gutterBuffer = (size: number) => `<div data-gutter-buffer="buffer" data-buffer-size="${size}" style="grid-row: span ${size};"></div>`;
  const deletions = {
    gutter: [1, 2, 3, 4, 5, 6].map(gutter).join(""),
    content: content(1, "context", 1) + content(2, "change-deletion") + content(3, "change-deletion")
      + content(4, "change-deletion") + content(5, "context", 3) + content(6, "context", 4),
  };
  const additions = {
    gutter: gutter(1) + gutter(2) + gutterBuffer(2) + gutter(3) + gutter(4),
    content: content(1, "context", 1) + content(2, "change-addition") + contentBuffer(2)
      + content(3, "context", 5) + content(4, "context", 6),
  };
  const column = (side: string, part: { gutter: string; content: string }) => (
    `<code data-code data-${side}><div data-gutter>${part.gutter}</div><div data-content>${part.content}</div></code>`
  );
  const item = document.createElement("div");
  const host = document.createElement("diffs-container");
  item.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<pre>${column("deletions", deletions)}${column("additions", additions)}</pre>`;
  document.body.appendChild(item);
  return { item, root };
}

describe("decorateReviewTargetFolds on Pierre's split grid", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // A (folded) is old 2-3 and new 2-3; B (open) is old 4.
  const folded = hunk("A", [
    { side: "old", start_line: 2, end_line: 3, hunk_ordinal: 0 },
    { side: "new", start_line: 2, end_line: 3, hunk_ordinal: 0 },
  ]);
  const open = hunk("B", [{ side: "old", start_line: 4, end_line: 4, hunk_ordinal: 0 }]);

  function visible(part: Element): string[] {
    return [...part.children]
      .map((child) => child as HTMLElement)
      .filter((child) => !isFoldHiddenRow(child))
      .map((child) => child.dataset.line ?? child.dataset.columnNumber ?? (child.dataset.lcReviewFoldShrunk ? "buffer:shrunk" : "buffer"));
  }

  it("hides whole grid rows in both columns, line numbers included, so the columns stay in step", () => {
    const { item, root } = splitGrid();
    const [deletions, additions] = [...root.querySelectorAll("[data-code]")];

    decorateReviewTargetFolds(item, "src/app.py", [folded, open], new Set(["A"]), "update");

    // Rows 1, 2 and 4 are A's alone: row 4's context row is new line 3 in both
    // columns, although the deletions column numbers it old line 5.
    expect(visible(deletions.querySelector("[data-content]")!)).toEqual(["1", "4", "6"]);
    expect(visible(deletions.querySelector("[data-gutter]")!)).toEqual(["1", "4", "6"]);
    expect(visible(additions.querySelector("[data-content]")!)).toEqual(["1", "buffer:shrunk", "4"]);
    expect(visible(additions.querySelector("[data-gutter]")!)).toEqual(["1", "buffer:shrunk", "4"]);
  });

  it("keeps only the open rows of a buffer that spans folded and open rows, and restores it on unfold", () => {
    const { item, root } = splitGrid();
    const buffer = root.querySelector<HTMLElement>("[data-additions] [data-content-buffer]")!;
    const originalStyle = buffer.getAttribute("style");

    decorateReviewTargetFolds(item, "src/app.py", [folded, open], new Set(["A"]), "update");
    expect(buffer.dataset.lcReviewFoldShrunk).toBe("true");
    expect(buffer.style.gridRow).toBe("span 1");

    decorateReviewTargetFolds(item, "src/app.py", [folded, open], new Set(), "update");
    expect(buffer.getAttribute("style")).toBe(originalStyle);
    expect(buffer.dataset.lcReviewFoldShrunk).toBeUndefined();
    expect(root.querySelectorAll("[data-lc-review-fold-hidden]")).toHaveLength(0);
  });
});
