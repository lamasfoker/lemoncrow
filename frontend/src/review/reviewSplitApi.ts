/**
 * Reviewer-requested split into stacked Changes (Phase 12 item 13) over
 * `/api/reviews/{id}/split-requests`.
 *
 * Same contract as Fix: preview first (plan, route, exact handoff), then start
 * with that preview's `operation_id` and handoff digest. Collecting turns the
 * agent's stacked branches into linked Reviews; the original stays open.
 */
import { requestBrowserJson } from "./reviewApi";
import type { ReviewActionEvent, ReviewActionRoute, ReviewActionRouteTier } from "./reviewActionsApi";

export type ReviewSplitStatus =
  | "dispatching"
  | "awaiting_branches"
  | "uncertain"
  | "collecting"
  | "completed"
  | "failed"
  | "cancelled";

export const SPLIT_COLLECTABLE: ReadonlySet<ReviewSplitStatus> = new Set(["awaiting_branches", "uncertain"]);
export const SPLIT_CANCELLABLE: ReadonlySet<ReviewSplitStatus> = new Set(["dispatching", "awaiting_branches", "uncertain"]);

export interface ReviewSplitLayer {
  index: number;
  cohort_id: string;
  title: string;
  paths: string[];
  branch: string;
}

export interface ReviewSplitPlan {
  base_sha: string;
  head_sha: string;
  layers: ReviewSplitLayer[];
  digest: string;
}

export interface ReviewSplitChild {
  layer: number;
  cohort_id: string;
  branch: string;
  base_sha: string;
  head_sha: string;
  review_id: string;
  revision_id: string;
  title: string;
  status: string;
  extra_paths: string[];
  missing_paths: string[];
}

export interface ReviewSplitMigration {
  carried: number;
  per_child: Record<string, number>;
  not_carried_total: number;
  not_carried: Array<{ reviewer_id: string; unit_key: string; reason: string }>;
}

export interface ReviewSplitRecord {
  id: string;
  review_id: string;
  source_revision_id: string;
  source_revision_number: number | null;
  status: ReviewSplitStatus;
  route: ReviewActionRoute;
  plan: ReviewSplitPlan;
  handoff_digest: string;
  directive: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  message: string;
  events: Array<Omit<ReviewActionEvent, "revision_id">>;
  children: ReviewSplitChild[];
  migration: ReviewSplitMigration | null;
}

export interface ReviewSplitOrigin {
  review_id: string;
  split_id: string;
  layer: number | null;
  layer_count: number | null;
  branch: string | null;
}

export interface ReviewSplitListing {
  splits: ReviewSplitRecord[];
  split_from: ReviewSplitOrigin[];
  collect_available: boolean;
  collect_reason: string;
}

export interface ReviewSplitPreview {
  revision_id: string;
  operation_id: string;
  ready: boolean;
  reason: string;
  plan: ReviewSplitPlan | null;
  route: ReviewActionRoute | null;
  candidates: ReviewActionRoute[];
  handoff: string;
  handoff_digest: string;
  collect_available: boolean;
  collect_reason: string;
}

export interface ReviewSplitRequest {
  route?: ReviewActionRouteTier;
  directive?: string;
}

function splitPath(reviewId: string, suffix = ""): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/split-requests${suffix}`;
}

function requestBody(request: ReviewSplitRequest): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (request.route) body.route = request.route;
  if (request.directive?.trim()) body.directive = request.directive.trim();
  return body;
}

export function fetchReviewSplits(reviewId: string): Promise<ReviewSplitListing> {
  return requestBrowserJson<ReviewSplitListing>(splitPath(reviewId));
}

export function previewReviewSplit(
  reviewId: string,
  expectedRevisionId: string,
  request: ReviewSplitRequest,
): Promise<ReviewSplitPreview> {
  return requestBrowserJson<ReviewSplitPreview>(splitPath(reviewId, "/preview"), {
    method: "POST",
    body: JSON.stringify({ ...requestBody(request), expected_revision_id: expectedRevisionId }),
  });
}

export function startReviewSplit(
  reviewId: string,
  request: ReviewSplitRequest,
  preview: ReviewSplitPreview,
  options: { copied?: boolean } = {},
): Promise<{ split: ReviewSplitRecord; replayed: boolean }> {
  const body: Record<string, unknown> = {
    ...requestBody(request),
    expected_revision_id: preview.revision_id,
    operation_id: preview.operation_id,
    expected_handoff_digest: preview.handoff_digest,
  };
  if (options.copied) body.copied = true;
  return requestBrowserJson(splitPath(reviewId), { method: "POST", body: JSON.stringify(body) });
}

export function collectReviewSplit(
  reviewId: string,
  splitId: string,
  expectedPlanDigest: string,
): Promise<{ split: ReviewSplitRecord }> {
  return requestBrowserJson(splitPath(reviewId, `/${encodeURIComponent(splitId)}/collect`), {
    method: "POST",
    body: JSON.stringify(expectedPlanDigest ? { expected_plan_digest: expectedPlanDigest } : {}),
  });
}

export function cancelReviewSplit(reviewId: string, splitId: string, reason = ""): Promise<{ split: ReviewSplitRecord }> {
  return requestBrowserJson(splitPath(reviewId, `/${encodeURIComponent(splitId)}/cancel`), {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  });
}
