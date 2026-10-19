/**
 * Imported deterministic findings (SARIF, check-run annotations) for one Review Snapshot.
 *
 * Findings are producer data: messages are shown as text, never as markup or
 * instructions. Stale findings come from an earlier Snapshot and are context
 * only; chains compose current findings for display and never gate readiness.
 */
import { requestBrowserJson } from "./reviewApi";

export type FindingSeverity = "p0" | "p1" | "p2" | "suggestion";
export type StaticDisposition = "dismissed" | "confirmed" | "open";
export type StaticDismissalReason = "incorrect" | "accepted_risk" | "duplicate" | "not_applicable";

export interface StaticFindingDisposition {
  disposition: string;
  reason: string;
  actor: string;
  snapshot_id: string;
  recorded_at: string;
}

export interface StaticFinding {
  id: string;
  /** Line-independent identity; a disposition follows it to later Snapshots. */
  fingerprint: string;
  ref: string;
  tool: string;
  rule_id: string;
  severity: FindingSeverity;
  severity_label: string;
  level: string;
  path: string;
  start_line: number;
  end_line: number;
  message: string;
  message_truncated: boolean;
  tags: string[];
  cwe: number[];
  in_snapshot: boolean;
  state: "current" | "stale";
  evidence_id: string;
  snapshot_id: string;
  snapshot_number: number | null;
  check: string;
  check_identity: string;
  check_status: string;
  category: string;
  attestation_class: string;
  /** attested (typed provider/LemonCrow result), human, or reported (caller-attested). */
  authority: string;
  head_sha: string;
  disposition: StaticFindingDisposition | null;
}

export interface StaticFindingChainMember {
  ref: string;
  finding_id: string;
  evidence_id: string;
  check_identity: string;
  tool: string;
  rule_id: string;
  severity: FindingSeverity;
  path: string;
  start_line: number;
  facts: string[];
}

export interface StaticFindingChain {
  id: string;
  rule: { id: string; title: string; version: number; needs: string[]; severity: FindingSeverity };
  title: string;
  path: string;
  severity: FindingSeverity;
  severity_label: string;
  contributing_max: FindingSeverity;
  raised: boolean;
  members: StaticFindingChainMember[];
  edges: Array<{ from: string; to: string; relation: string }>;
  explanation: string[];
  /** Always false: a chain is display only. */
  gates: boolean;
}

export interface StaticFindingsCheck {
  evidence_id: string;
  check: string;
  check_identity: string;
  tool: string;
  tool_version: string;
  category: string;
  format: string;
  status: string;
  status_basis: string;
  state: "current" | "stale";
  snapshot_number: number | null;
  findings: number;
  truncation: { truncated: boolean; note: string; total: number; kept: number };
}

export interface StaticFindingsProjection {
  review_id: string;
  snapshot_id: string;
  path: string;
  findings: StaticFinding[];
  stale: StaticFinding[];
  checks: StaticFindingsCheck[];
  chains: StaticFindingChain[];
  truncation_notes: string[];
  counts: { current: number; stale: number; dismissed: number; chains: number };
  latest: boolean;
}

export async function fetchStaticFindings(
  reviewId: string,
  revisionId: string,
  path: string,
): Promise<StaticFindingsProjection> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  if (path) params.set("path", path);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return requestBrowserJson<StaticFindingsProjection>(
    `/api/reviews/${encodeURIComponent(reviewId)}/static-findings${query}`,
  );
}

export async function postStaticDisposition(
  reviewId: string,
  revisionId: string,
  findingId: string,
  disposition: StaticDisposition,
  reason: StaticDismissalReason | "" = "",
): Promise<{ disposition: Record<string, unknown>; static_findings: StaticFindingsProjection }> {
  return requestBrowserJson(
    `/api/reviews/${encodeURIComponent(reviewId)}/static-findings/${encodeURIComponent(findingId)}/disposition`,
    { method: "POST", body: JSON.stringify({ revision_id: revisionId, disposition, reason }) },
  );
}
