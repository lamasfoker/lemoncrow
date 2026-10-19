/**
 * P12-AUTO client: discard safety, automation rules + post-merge actions, and
 * published-feedback sync. Every mutation is an explicit human request; the
 * server decides and records, this module only carries its answers.
 */
import { requestBrowserJson } from "./reviewApi";

// ----- discard / delete ---------------------------------------------------- //

export type ArchiveHazardId = "pending_feedback" | "armed_landing";

export interface ArchiveHazard {
  id: ArchiveHazardId;
  label: string;
  count: number;
  detail: string;
  /** What the discard does to it, in the words the confirmation shows. */
  outcome: string;
  subject_ids: string[];
  provider: string;
  owed_cancellation: boolean;
}

export interface OwedCancellation {
  provider: string;
  landing_status: string;
  reason: string;
  detail: string;
  error: string;
}

export interface ArchiveHazards {
  code?: string;
  message?: string;
  review_id: string;
  hazards: ArchiveHazard[];
  hazard_ids: ArchiveHazardId[];
  hazard_digest: string;
  confirmation_required: boolean;
  deletes_now: boolean;
  retention: string;
  owed_cancellation?: OwedCancellation | null;
}

export interface ArchiveConfirmation {
  acknowledged_hazards: string[];
  hazard_digest: string;
}

/** The hazards a refused discard named, or null when the error is something else. */
export function archiveConfirmationRequired(error: unknown): ArchiveHazards | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<ArchiveHazards>;
  if (candidate.code !== "archive_confirmation_required" || !Array.isArray(candidate.hazards)) return null;
  return candidate as ArchiveHazards;
}

export function confirmationFor(hazards: ArchiveHazards): ArchiveConfirmation {
  return { acknowledged_hazards: [...hazards.hazard_ids], hazard_digest: hazards.hazard_digest };
}

/** Exactly what the server accepts as a hard-delete confirmation. */
export function deleteIdentityMatches(typed: string, id: string, title: string): boolean {
  const value = typed.trim();
  if (!value) return false;
  return value === id || value === `r/${id}` || (title.trim() !== "" && value === title.trim());
}

// ----- automation rules + post-merge -------------------------------------- //

export interface AutomationCheck {
  name: string;
  kind: "guard" | "condition";
  passed: boolean;
  detail: string;
  fact: string;
  op: string;
  expected: unknown;
  actual: unknown;
}

export interface BranchProtectionView {
  requirements: "satisfied" | "unsatisfied" | "unknown";
  required_reviews: "satisfied" | "unsatisfied" | "unknown";
  changes_requested: boolean;
  source: string;
  detail: string;
  rows: Array<{ id: string; label: string; state: string }>;
}

export interface AutomationRuleView {
  id: string;
  description: string;
  trigger: string;
  action: string;
  action_label: string;
  state_changing: boolean;
  reserved: boolean;
  permission: string;
  enabled: boolean;
  enabled_by: string;
  enabled_at?: string;
  layer: string;
  source: string;
  acts_as: { subject: string; role: string };
  repositories: string[];
  conditions: Array<{ fact: string; op: string; value: unknown }>;
  params: Record<string, unknown>;
}

export interface RuleEvaluationView {
  rule_id: string;
  action: string;
  trigger: string;
  would_fire: boolean;
  reason: string;
  guards: AutomationCheck[];
  conditions: AutomationCheck[];
  branch_protection: BranchProtectionView;
  branch_protection_enforced: boolean;
}

export interface AutomationDecisionView {
  action: string;
  mode: "rule" | "needs_human" | "refused";
  allowed: boolean;
  reason: string;
  rule_id: string;
  permission: string;
  acts_as: { subject: string; role: string } | null;
}

export interface PostMergeRecordView {
  id: string;
  kind: string;
  label: string;
  status: "running" | "completed" | "failed" | "unavailable" | "refused";
  initiated_by: "human" | "rule";
  actor_id: string;
  rule_id: string;
  acts_as: { subject?: string; role?: string };
  permission: string;
  detail: string;
  output: Record<string, unknown>;
  readiness_unchanged: boolean;
  attempts: number;
  updated_at?: string;
}

export interface PostMergeKindView {
  kind: string;
  action: string;
  label: string;
  permission: string;
  state_changing: boolean;
  latest: PostMergeRecordView | null;
  runnable: boolean;
}

export interface ReviewAutomationView {
  review_id: string;
  repository: string;
  policy: { rules: AutomationRuleView[]; errors: string[]; source: string; edit_source: string };
  facts: { branch_protection: BranchProtectionView };
  evaluations: RuleEvaluationView[];
  decisions: AutomationDecisionView[];
  merge: { source: string; provider: string; merged_head: string; reviewed_head_merged: boolean; detail: string } | null;
  post_merge: {
    available: boolean;
    reason: string;
    in_readiness: boolean;
    kinds: PostMergeKindView[];
    records: PostMergeRecordView[];
  };
  readiness_digest: string;
}

export function fetchReviewAutomation(reviewId: string): Promise<ReviewAutomationView> {
  return requestBrowserJson<ReviewAutomationView>(`/api/reviews/${encodeURIComponent(reviewId)}/automation`);
}

export function runPostMergeAction(reviewId: string, kind: string): Promise<{ record: PostMergeRecordView; ran: boolean }> {
  return requestBrowserJson(`/api/reviews/${encodeURIComponent(reviewId)}/post-merge/${encodeURIComponent(kind)}`, {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
}

// ----- published feedback sync -------------------------------------------- //

export type FeedbackSyncState = "sync_pending" | "synced" | "corrected" | "sync_failed" | "abandoned";

export interface FeedbackSyncRecordView {
  id: string;
  subject_id: string;
  remote_ref: string;
  remote_kind: "comment" | "review";
  provider: string;
  op: "edit" | "withdraw" | "dismiss";
  state: FeedbackSyncState;
  requested_body: string;
  local_body: string;
  error: string;
  attempts: number;
  updated_at: string;
}

export interface PublishedRefView {
  annotation_id: string;
  remote_ref: string;
  kind: "comment" | "review";
  remote_id: number;
  target_ref: string;
}

export interface CommentSyncView {
  published: PublishedRefView | null;
  sync: FeedbackSyncRecordView | null;
}

export interface FeedbackSyncView {
  review_id: string;
  comments: Record<string, CommentSyncView>;
  reviews: FeedbackSyncRecordView[];
}

export type FeedbackSyncOp = "edit" | "withdraw" | "retry" | "discard";

export function fetchFeedbackSync(reviewId: string): Promise<FeedbackSyncView> {
  return requestBrowserJson<FeedbackSyncView>(`/api/reviews/${encodeURIComponent(reviewId)}/feedback-sync`);
}

export function syncPublishedComment(
  annotationId: string,
  op: FeedbackSyncOp,
  body = "",
): Promise<{ sync: FeedbackSyncRecordView; annotation_id: string; local_changed?: boolean }> {
  return requestBrowserJson(`/api/annotations/${encodeURIComponent(annotationId)}/sync`, {
    method: "POST",
    body: JSON.stringify(op === "edit" ? { op, body } : { op }),
  });
}
