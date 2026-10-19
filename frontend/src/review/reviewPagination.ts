/** Changed source lines per scroll page; files remain indivisible. */
export const REVIEW_PAGE_CHANGE_BUDGET = 2000;

export function readReviewPage(value: string | null): number {
  if (!value || !/^[1-9]\d*$/.test(value)) return 1;
  const page = Number(value);
  return Number.isSafeInteger(page) ? page : 1;
}

export function reviewPages(
  paths: readonly string[],
  weights: ReadonlyMap<string, number>,
  budget = REVIEW_PAGE_CHANGE_BUDGET,
): string[][] {
  const pages: string[][] = [];
  let page: string[] = [];
  let lines = 0;
  for (const path of paths) {
    // Binary/metadata-only files still take space and must remain reachable.
    const weight = Math.max(1, weights.get(path) ?? 1);
    if (page.length && lines + weight > budget) {
      pages.push(page);
      page = [];
      lines = 0;
    }
    page.push(path);
    lines += weight;
  }
  if (page.length) pages.push(page);
  return pages;
}
