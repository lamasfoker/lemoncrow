/**
 * Human approval of risky authoring-agent actions (Phase 12 item 14) over
 * `/api/reviews/{id}/agent-approvals`.
 *
 * The agent host's hook files a request and waits; only a signed-in human
 * decides here. A decision names the exact action it decides (its digest), so
 * an approval can never land on a different command than the one shown.
 */
import { requestBrowserJson } from "./reviewApi";

export type AgentApprovalStatus = "pending" | "approved" | "denied" | "expired";

export interface AgentApproval {
  id: string;
  status: AgentApprovalStatus;
  /** A one-time approval the agent has already used. */
  used: boolean;
  risk_class: string;
  risk_label: string;
  classes: string[];
  kind: "command" | "write" | string;
  action: string;
  action_digest: string;
  cwd: string;
  host: string;
  session_id: string;
  requested_by: string;
  requested_at: string;
  expires_at: string;
  decided_by: string;
  decided_at: string;
  scope: "" | "once" | "pattern";
  pattern: string;
  valid_until: string;
  claimed_at: string;
  note: string;
  revision_id: string;
}

/** How an observed side effect relates to a human approval; `null` when none was required. */
export type AgentActionApproval =
  | {
      status: "pre_approved";
      approval_id: string;
      decided_by: string;
      decided_at: string;
      scope: string;
      pattern: string;
    }
  | { status: "unapproved" }
  | null;

export interface AgentApprovalListing {
  approvals: AgentApproval[];
  total: number;
  policy: { classes: string[]; wait_seconds: number } | null;
  labels: Record<string, string>;
  limits: { min_ttl_seconds: number; max_ttl_seconds: number; default_ttl_seconds: number };
}

export interface AgentApprovalDecision {
  decision: "approve" | "deny";
  note?: string;
}

function approvalsPath(reviewId: string, suffix = ""): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/agent-approvals${suffix}`;
}

export function fetchAgentApprovals(reviewId: string): Promise<AgentApprovalListing> {
  return requestBrowserJson<AgentApprovalListing>(approvalsPath(reviewId));
}

/** Approve exactly this action once, or deny it. Patterns are an API-only, explicit choice. */
export function decideAgentApproval(
  reviewId: string,
  approval: Pick<AgentApproval, "id" | "action_digest">,
  decision: AgentApprovalDecision,
): Promise<{ approval: AgentApproval; development_recorded: boolean }> {
  const body: Record<string, unknown> = {
    decision: decision.decision,
    expected_action_digest: approval.action_digest,
  };
  if (decision.decision === "approve") body.scope = "once";
  if (decision.note?.trim()) body.note = decision.note.trim();
  return requestBrowserJson(approvalsPath(reviewId, `/${encodeURIComponent(approval.id)}/decision`), {
    method: "POST",
    body: JSON.stringify(body),
  });
}
