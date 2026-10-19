import type { Draft } from "./annotationModel";

const COMMENT_PREFIX = "lc.review.comment-draft";
const ASK_PREFIX = "lc.review.ask-draft";

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function safeToken(value: string): string {
  return encodeURIComponent(value);
}

export function commentDraftKey(reviewId: string, revisionId: string): string {
  return `${COMMENT_PREFIX}.${safeToken(reviewId)}.${safeToken(revisionId)}`;
}

export function saveCommentDraft(reviewId: string, revisionId: string, draft: Draft | null): void {
  const store = storage();
  if (!store || !reviewId || !revisionId) return;
  const key = commentDraftKey(reviewId, revisionId);
  if (!draft) {
    store.removeItem(key);
    return;
  }
  // Proposal/source-edit mechanics are intentionally excluded. This record is
  // only unsent human feedback bound to one exact Review/Snapshot/anchor.
  const payload: Draft = {
    path: draft.path,
    startLine: draft.startLine,
    endLine: draft.endLine,
    side: draft.side,
    fileLevel: draft.fileLevel,
    targetUnitKey: draft.targetUnitKey,
    body: draft.body ?? "",
    kind: draft.kind ?? "comment",
    markTarget: draft.markTarget ?? true,
    recovered: draft.recovered,
  };
  store.setItem(key, JSON.stringify(payload));
}

export function loadCommentDraft(reviewId: string, revisionId: string): Draft | null {
  const store = storage();
  if (!store || !reviewId || !revisionId) return null;
  try {
    const raw = JSON.parse(store.getItem(commentDraftKey(reviewId, revisionId)) ?? "null") as Partial<Draft> | null;
    if (
      !raw
      || typeof raw.path !== "string"
      || typeof raw.startLine !== "number"
      || typeof raw.endLine !== "number"
      || (raw.side !== "additions" && raw.side !== "deletions")
    ) return null;
    return {
      path: raw.path,
      startLine: raw.startLine,
      endLine: raw.endLine,
      side: raw.side,
      fileLevel: Boolean(raw.fileLevel),
      targetUnitKey: typeof raw.targetUnitKey === "string" ? raw.targetUnitKey : undefined,
      body: typeof raw.body === "string" ? raw.body : "",
      kind: raw.kind === "request_change" || raw.kind === "suggestion" || raw.kind === "looks_good" ? raw.kind : "comment",
      markTarget: raw.markTarget !== false,
      recovered: Boolean(raw.recovered),
    };
  } catch {
    return null;
  }
}

export interface AskDraftScope {
  target_unit_key?: string;
  path?: string;
  symbol?: string;
  side?: string;
  start_line?: number;
  end_line?: number;
}

export function askDraftKey(reviewId: string, revisionId: string, scope: AskDraftScope | null | undefined): string {
  const anchor = JSON.stringify({
    target_unit_key: scope?.target_unit_key ?? "",
    path: scope?.path ?? "",
    symbol: scope?.symbol ?? "",
    side: scope?.side ?? "",
    start_line: scope?.start_line ?? 0,
    end_line: scope?.end_line ?? 0,
  });
  return `${ASK_PREFIX}.${safeToken(reviewId)}.${safeToken(revisionId)}.${safeToken(anchor)}`;
}

export function saveAskDraft(
  reviewId: string,
  revisionId: string,
  scope: AskDraftScope | null | undefined,
  body: string,
): void {
  const store = storage();
  if (!store || !reviewId || !revisionId) return;
  const key = askDraftKey(reviewId, revisionId, scope);
  if (!body) store.removeItem(key);
  else store.setItem(key, body);
}

export function loadAskDraft(
  reviewId: string,
  revisionId: string,
  scope: AskDraftScope | null | undefined,
): string {
  const store = storage();
  if (!store || !reviewId || !revisionId) return "";
  try {
    return store.getItem(askDraftKey(reviewId, revisionId, scope)) ?? "";
  } catch {
    return "";
  }
}
