import { REVIEW_MAP_PARAMS } from "./reviewMapLocation";

export type DedicatedReviewItem =
  | { kind: "file"; path: string; targetId?: string }
  | { kind: "surface"; provider: string; id: string };

const ITEM_KIND = "review-item";
const ITEM_PATH = "review-path";
const ITEM_TARGET = "review-target";
const ITEM_PROVIDER = "review-provider";
const ITEM_SURFACE = "review-surface";
const ITEM_RETURN = "review-return";
const RETURN_PATH = "review-return-path";
const RETURN_SURFACE = "review-return-surface";

export const DEDICATED_REVIEW_ITEM_PARAMS = [ITEM_KIND, ITEM_PATH, ITEM_TARGET, ITEM_PROVIDER, ITEM_SURFACE] as const;
export const REVIEW_RETURN_PARAMS = [ITEM_RETURN, RETURN_PATH, RETURN_SURFACE] as const;

export function readDedicatedReviewItem(): DedicatedReviewItem | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const kind = params.get(ITEM_KIND);
  if (kind === "file") {
    const path = params.get(ITEM_PATH) ?? "";
    const targetId = params.get(ITEM_TARGET) ?? "";
    if (!path) return null;
    return targetId ? { kind: "file", path, targetId } : { kind: "file", path };
  }
  if (kind === "surface") {
    const provider = params.get(ITEM_PROVIDER) ?? "";
    const id = params.get(ITEM_SURFACE) ?? "";
    return provider && id ? { kind: "surface", provider, id } : null;
  }
  return null;
}

export function dedicatedReviewItemUrl(item: DedicatedReviewItem): string {
  const origin = new URL(window.location.href);
  const returnUrl = new URL(origin.href);
  if (!returnUrl.searchParams.has(REVIEW_MAP_PARAMS[0])) {
    if (item.kind === "file") returnUrl.searchParams.set(RETURN_PATH, item.path);
    else returnUrl.searchParams.set(RETURN_SURFACE, `${item.provider}:${item.id}`);
  }
  const url = new URL(origin.href);
  // The dedicated tab shows one item; its return link keeps this tab's map or file position.
  for (const key of [...DEDICATED_REVIEW_ITEM_PARAMS, ...REVIEW_MAP_PARAMS, RETURN_PATH, RETURN_SURFACE, ITEM_RETURN]) url.searchParams.delete(key);
  // Keep the auth fragment out of the query string; the dedicated URL already carries it.
  url.searchParams.set(ITEM_RETURN, `${returnUrl.pathname}${returnUrl.search}`);
  url.searchParams.set(ITEM_KIND, item.kind);
  if (item.kind === "file") {
    url.searchParams.set(ITEM_PATH, item.path);
    if (item.targetId) url.searchParams.set(ITEM_TARGET, item.targetId);
  } else {
    url.searchParams.set(ITEM_PROVIDER, item.provider);
    url.searchParams.set(ITEM_SURFACE, item.id);
  }
  return url.toString();
}

export function fullReviewUrlFromDedicatedItem(): string {
  const url = new URL(window.location.href);
  const returnLocation = url.searchParams.get(ITEM_RETURN);
  if (returnLocation) {
    const target = new URL(returnLocation, url.origin);
    if (target.origin === url.origin && (target.pathname === url.pathname || target.pathname === "/review")) {
      return `${target.pathname}${target.search}${url.hash}`;
    }
  }
  for (const key of [...DEDICATED_REVIEW_ITEM_PARAMS, ITEM_RETURN]) url.searchParams.delete(key);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function readReviewReturnPath(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(RETURN_PATH);
}

export function readReviewReturnSurface(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(RETURN_SURFACE);
}

export function openDedicatedReviewItem(item: DedicatedReviewItem): void {
  window.open(dedicatedReviewItemUrl(item), "_blank", "noopener,noreferrer");
}
