/**
 * The multi-repository change set of one Review (Phase 9R item 42).
 *
 * Members are linked Changes in other repositories; each keeps its own Review
 * and Snapshot. The set is ready only when every member is ready and the
 * contract and deploy-order checks pass. A member the viewer cannot read is
 * counted, never named, and makes the set `incomplete`.
 */
import { requestBrowserJson } from "./reviewApi";

export type ChangeSetState = "not_linked" | "incomplete" | "blocked" | "needs_decision" | "ready";

export interface ChangeSetMember {
  alias: string;
  readable: boolean;
  repository?: string;
  review_id?: string;
  ref?: string;
  title?: string;
  status?: string;
  snapshot_number?: number;
  current?: boolean;
  /** `null`: the member's own readiness could not be read. */
  ready?: boolean | null;
  blockers?: number;
  error?: string;
}

export interface ChangeSetStep {
  position: number;
  alias: string;
  repository: string;
  review_id: string;
  phase: "migrations" | "code";
  after: { alias: string; phase: string; reason: string }[];
}

export interface ChangeSetView {
  available: boolean;
  linked: boolean;
  state: ChangeSetState;
  members: ChangeSetMember[];
  member_count: number;
  unreadable: number;
  truncated: boolean;
  max_members: number;
  /** Repositories with more than one Change in the set (an update that stopped half way): blocked until unlinked. */
  duplicate_repositories?: string[];
  ready_members: number;
  contracts: {
    state: "compatible" | "incompatible" | "unknown" | "not_applicable";
    issues: { provider: string; consumer: string; contract: string; paths: string[] }[];
    notes: string[];
  };
  deploy_order: {
    state: "ordered" | "needs_decision" | "unknown";
    steps: ChangeSetStep[];
    cycle: { alias: string; phase: string }[];
  };
}

export function fetchChangeSet(reviewId: string): Promise<ChangeSetView> {
  return requestBrowserJson<ChangeSetView>(`/api/reviews/${encodeURIComponent(reviewId)}/change-set`);
}
