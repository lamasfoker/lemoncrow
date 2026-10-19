import { describe, expect, it } from "vitest";
import { readReviewPage, reviewPages } from "./reviewPagination";

describe("review pagination", () => {
  it("fills the line budget exactly and moves the next file to another page", () => {
    expect(reviewPages(["a", "b", "c"], new Map([["a", 1200], ["b", 800], ["c", 1]])))
      .toEqual([["a", "b"], ["c"]]);
  });
  it("keeps an oversized file intact on its own page without losing its neighbours", () => {
    expect(reviewPages(["a", "huge", "b"], new Map([["a", 1], ["huge", 3000], ["b", 1]])))
      .toEqual([["a"], ["huge"], ["b"]]);
  });
  it("keeps zero-line and unknown files reachable and handles an empty review", () => {
    const paths = Array.from({ length: 2001 }, (_, index) => String(index));
    const pages = reviewPages(paths, new Map(paths.map((path) => [path, 0])));
    expect(pages.map((page) => page.length)).toEqual([2000, 1]);
    expect(pages.flat()).toEqual(paths);
    expect(reviewPages(["unknown"], new Map())).toEqual([["unknown"]]);
    expect(reviewPages([], new Map())).toEqual([]);
  });
  it("uses one-based safe page numbers", () => {
    expect(readReviewPage("2")).toBe(2);
    for (const value of [null, "", "0", "-1", "2.5", "02", "abc", "9007199254740992"]) {
      expect(readReviewPage(value)).toBe(1);
    }
  });
});
