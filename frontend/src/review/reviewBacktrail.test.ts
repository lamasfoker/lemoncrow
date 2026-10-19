import { describe, expect, it } from "vitest";

import {
  backtrailQuery,
  pushReviewBacktrail,
  readReviewBacktrail,
  REVIEW_BACKTRAIL_LIMIT,
  REVIEW_BACKTRAIL_PARAM,
  writeReviewBacktrail,
  type ReviewBacktrailEntry,
} from "./reviewBacktrail";

function entry(overrides: Partial<ReviewBacktrailEntry> = {}): ReviewBacktrailEntry {
  return {
    query: "view=tour&guided=cohort%3Aauth",
    view: "tour",
    target: "target-3",
    scroll: 240,
    related: null,
    label: "Guided review",
    ...overrides,
  };
}

describe("review backtrail", () => {
  it("round-trips exact locations through the URL, including non-ASCII paths", () => {
    const trail = [
      entry(),
      entry({ query: "map=file&map-focus=file%3A0", view: "map", target: "", scroll: 0, label: "Map" }),
      entry({ query: "p=2", view: "implementation", related: { path: "src/été.py", line: 44 }, label: "src/été.py:L44" }),
    ];
    const params = writeReviewBacktrail(new URLSearchParams("view=brief"), trail);
    // Survives serialisation into a real URL and back (reload).
    const reloaded = new URL(`https://review.test/r/review-1?${params.toString()}`);
    expect(readReviewBacktrail(reloaded.searchParams)).toEqual(trail);
    expect(reloaded.searchParams.get("view")).toBe("brief");
  });

  it("keeps only the newest entries past the bound", () => {
    let trail: ReviewBacktrailEntry[] = [];
    for (let index = 0; index < REVIEW_BACKTRAIL_LIMIT + 5; index += 1) {
      trail = pushReviewBacktrail(trail, entry({ target: `target-${index}` }));
    }
    expect(trail).toHaveLength(REVIEW_BACKTRAIL_LIMIT);
    expect(trail[0].target).toBe("target-5");
    expect(trail.at(-1)?.target).toBe(`target-${REVIEW_BACKTRAIL_LIMIT + 4}`);
    // A trail at the bound still decodes; one past it is refused whole.
    expect(readReviewBacktrail(writeReviewBacktrail(new URLSearchParams(), trail))).toHaveLength(REVIEW_BACKTRAIL_LIMIT);
    const oversized = writeReviewBacktrail(new URLSearchParams(), [...trail, entry()]);
    expect(readReviewBacktrail(oversized)).toEqual([]);
  });

  it("replaces rather than duplicates a re-jump from the place just recorded", () => {
    const first = pushReviewBacktrail([], entry({ scroll: 10 }));
    const again = pushReviewBacktrail(first, entry({ scroll: 30 }));
    expect(again).toEqual([entry({ scroll: 30 })]);
  });

  it("fails closed on a malformed or tampered trail", () => {
    const bad = (value: string) => readReviewBacktrail(new URLSearchParams({ [REVIEW_BACKTRAIL_PARAM]: value }));
    expect(bad("not-base64!!")).toEqual([]);
    expect(bad(btoa("{}"))).toEqual([]);
    expect(bad(btoa(JSON.stringify([["q", "settings", "", 0, "", 0, "x"]])))).toEqual([]);
    expect(bad(btoa(JSON.stringify([["q", "brief", "", -1, "", 0, "Brief"]])))).toEqual([]);
    // An entry that carries its own trail could loop; it is refused.
    expect(bad(btoa(JSON.stringify([[`${REVIEW_BACKTRAIL_PARAM}=x`, "brief", "", 0, "", 0, "Brief"]])))).toEqual([]);
    // One bad entry drops the whole trail rather than returning somewhere wrong.
    const good = ["view=brief", "brief", "", 0, "", 0, "Brief"];
    expect(bad(btoa(JSON.stringify([good])))).toHaveLength(1);
    expect(bad(btoa(JSON.stringify([good, ["q", "settings", "", 0, "", 0, "x"]])))).toEqual([]);
  });

  it("records the departure query without the trail and clears an emptied trail", () => {
    const params = writeReviewBacktrail(new URLSearchParams("view=brief"), [entry()]);
    expect(backtrailQuery(params)).toBe("view=brief");
    expect(pushReviewBacktrail([], entry({ query: "x=" + "a".repeat(2000) }))).toEqual([]);
    writeReviewBacktrail(params, []);
    expect(params.has(REVIEW_BACKTRAIL_PARAM)).toBe(false);
  });
});
