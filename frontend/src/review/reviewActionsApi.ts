/**
 * Controlled Review actions (Fix, Fix all and explicit corrections) over
 * `/api/reviews/{id}/actions`.
 *
 * An action is previewed first: the preview names the route (exact author ->
 * preferred agent -> copied prompt), what is included, what is excluded and
 * why, and the exact handoff. Starting sends that preview's `operation_id` and
 * handoff digest, so what dispatches is what the reviewer saw.
 */
import { requestBrowserJson } from "./reviewApi";

export type ReviewActionKind =
  | "fix"
  | "fix_all"
  | "fix_ci_failure"
  | "update_tests"
  | "resolve_merge_conflicts"
  | "apply_suggestion"
  | "create_follow_up_issue"
  | "org_recipe"
  | "revert"
  | "fix_forward";
export type ReviewActionStatus = "dispatching" | "awaiting_snapshot" | "uncertain" | "snapshot_recorded" | "failed" | "cancelled";
export type ReviewActionRouteTier = "exact_session" | "preferred_agent" | "prompt_fallback" | "reviewer_apply";
export type ReviewActionRouteMode = "resume" | "new_session" | "copy" | "apply";

export interface ReviewActionRoute {
  tier: ReviewActionRouteTier;
  mode: ReviewActionRouteMode;
  available: boolean;
  host: string;
  session_id: string;
  target_ref: string;
  label: string;
  reason: string;
}

export interface ReviewActionSubject {
  kind: string;
  id: string;
  title: string;
  path: string;
  start_line: number;
  end_line: number;
  revision_id: string;
  state: string;
  /** The subject's state now (listing only): finding -> action -> Snapshot -> disposition. */
  current?: { state?: string; disposition?: string; disposition_revision_id?: string; disposition_by?: string };
}

export interface ReviewActionExclusion {
  subject_id: string;
  kind: string;
  code: string;
  reason: string;
  title: string;
}

export interface ReviewActionEvent {
  kind: string;
  at: string;
  actor_type: string;
  actor_id: string;
  detail: string;
  revision_id: string;
}

export interface ReviewActionRerun {
  revision_id: string;
  available: boolean;
  reason?: string;
  incremental?: boolean;
  affected_paths?: string[] | null;
  plan_digest?: string;
  entries?: Array<{ id: string; title: string; status: string; reason: string; required: boolean }>;
}

export interface ReviewActionRecord {
  id: string;
  review_id: string;
  kind: ReviewActionKind;
  label: string;
  code_changing: boolean;
  source_revision_id: string;
  source_revision_number: number | null;
  route: ReviewActionRoute;
  status: ReviewActionStatus;
  created_by: string;
  created_at: string;
  updated_at: string;
  subjects: ReviewActionSubject[];
  excluded: ReviewActionExclusion[];
  directive: string;
  handoff_digest: string;
  identity: string;
  resulting_revision_id: string;
  resulting_revision_number: number | null;
  result_certainty: "" | "exact" | "source_fingerprint" | "unattributed";
  message: string;
  remote_ref: string;
  events: ReviewActionEvent[];
  version: number;
  rerun: ReviewActionRerun | null;
}

export interface ReviewActionAvailability {
  kind: ReviewActionKind;
  label: string;
  available: boolean;
  reason: string;
  setup: string;
  subject_ids: string[];
  code_changing: boolean;
}

export interface ReviewActionListing {
  revision_id: string;
  actions: ReviewActionRecord[];
  catalog: ReviewActionAvailability[];
  fix_all_limit: number;
}

export interface ReviewActionRequest {
  kind: ReviewActionKind;
  subject_ids: string[];
  route?: ReviewActionRouteTier;
  directive?: string;
}

export interface ReviewActionPreview {
  revision_id: string;
  kind: ReviewActionKind;
  label: string;
  operation_id: string;
  ready: boolean;
  included: ReviewActionSubject[];
  excluded: ReviewActionExclusion[];
  route: ReviewActionRoute | null;
  candidates: ReviewActionRoute[];
  handoff: string;
  handoff_digest: string;
  limit: number;
  code_changing: boolean;
}

function actionsPath(reviewId: string, suffix = ""): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/actions${suffix}`;
}

function requestBody(request: ReviewActionRequest): Record<string, unknown> {
  const body: Record<string, unknown> = { kind: request.kind, subject_ids: request.subject_ids };
  if (request.route) body.route = request.route;
  if (request.directive?.trim()) body.directive = request.directive.trim();
  return body;
}

export function fetchReviewActions(reviewId: string): Promise<ReviewActionListing> {
  return requestBrowserJson<ReviewActionListing>(actionsPath(reviewId));
}

export function previewReviewAction(
  reviewId: string,
  expectedRevisionId: string,
  request: ReviewActionRequest,
): Promise<ReviewActionPreview> {
  return requestBrowserJson<ReviewActionPreview>(actionsPath(reviewId, "/preview"), {
    method: "POST",
    body: JSON.stringify({ ...requestBody(request), expected_revision_id: expectedRevisionId }),
  });
}

export function startReviewAction(
  reviewId: string,
  request: ReviewActionRequest,
  preview: ReviewActionPreview,
  options: { copied?: boolean } = {},
): Promise<{ action: ReviewActionRecord; replayed: boolean }> {
  const body: Record<string, unknown> = {
    ...requestBody(request),
    expected_revision_id: preview.revision_id,
    operation_id: preview.operation_id,
    expected_handoff_digest: preview.handoff_digest,
  };
  if (options.copied) body.copied = true;
  return requestBrowserJson(actionsPath(reviewId), { method: "POST", body: JSON.stringify(body) });
}

export function cancelReviewAction(reviewId: string, actionId: string, reason = ""): Promise<{ action: ReviewActionRecord }> {
  return requestBrowserJson(actionsPath(reviewId, `/${encodeURIComponent(actionId)}/cancel`), {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  });
}
