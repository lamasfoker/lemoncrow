import { useSyncExternalStore } from "react";

/**
 * The phone composition (Phase 14 item 3): row 1 Back | title | More, row 2
 * the persistent views, content, then one sticky bottom dock with the primary
 * action and the Changes / Comments / Status / Ask entry points. Must match the
 * `max-width: 639px` block in reviewUi.css.
 */
export const REVIEW_COMPACT_QUERY = "(max-width: 639px)";

function compactQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(REVIEW_COMPACT_QUERY);
}

export function isCompactReviewViewport(): boolean {
  return Boolean(compactQuery()?.matches);
}

function subscribe(onChange: () => void): () => void {
  const query = compactQuery();
  if (!query) return () => undefined;
  query.addEventListener?.("change", onChange);
  return () => query.removeEventListener?.("change", onChange);
}

/** Live phone-composition flag; false wherever media queries are unavailable. */
export function useCompactReviewLayout(): boolean {
  return useSyncExternalStore(subscribe, isCompactReviewViewport, () => false);
}
