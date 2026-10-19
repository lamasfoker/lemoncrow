/**
 * Artifact and infrastructure analyses for one exact Review Snapshot (Phase 9R items 15, 31, 32, 33).
 *
 * Migrations, regenerate-and-compare, shipped artifacts and infrastructure
 * renders come only from the trusted base; they run sandboxed when the
 * effective run plan schedules them. Results become behavior changes; heavy
 * artifacts (schema diffs, rendered manifests, plans) are stored redacted.
 */
import { requestBrowserJson } from "./reviewApi";

export type AnalysisVerdict = "PASS" | "FAIL" | "UNKNOWN" | "NOT_RUN";

export interface AnalysisSpec {
  entry_id: string;
  id: string;
  tool: string;
  root: string;
  title: string;
  origin: "configured" | "discovered";
  source: string;
  executable: boolean;
  reason: string;
  setup_hint: string;
  digest: string;
}

export interface AnalysisEntryRow {
  id: string;
  title: string;
  config_key: string;
  status: string;
  reason: string;
  sides: string;
  required: boolean;
  setup_hint: string;
  configured: boolean;
  specs: AnalysisSpec[];
}

export interface AnalysisFinding {
  rule: string;
  category: string;
  subject: string;
  title: string;
  summary: string;
  severity: "high" | "medium" | "low";
  critical: boolean;
  change: "added" | "changed" | "removed";
  paths: string[];
}

export interface AnalysisArtifactRef {
  name: string;
  ref: string;
  digest: string;
  bytes: number;
  expires_at: number | null;
}

export interface AnalysisAttempt {
  side: "old" | "new";
  state: "ok" | "findings" | "unavailable" | "failed" | "error" | "not_run";
  reason: string;
  exit_code: number | null;
  evidence_id: string;
  evidence_ref: string;
  duration_ms: number;
  reused: boolean;
  environment: string;
  executor_class: string;
}

export interface AnalysisRecord {
  entry_id: string;
  entry_title: string;
  spec: AnalysisSpec;
  sides: string[];
  status: AnalysisVerdict;
  reasons: string[];
  setup_hint: string;
  findings: AnalysisFinding[];
  summary: Record<string, unknown>;
  attempts: AnalysisAttempt[];
  artifacts: AnalysisArtifactRef[];
  executor_class: string;
  reproduce: { command: string; evidence_ids: string[] };
  evidence_id: string;
  observed_at: string;
}

/** One mixed-version pairing of a compatibility record (Phase 9R item 41). */
export interface CompatPairing {
  pairing: string;
  label: string;
  direction: "forward" | "reverse";
  state: "compatible" | "incompatible" | "unknown" | "not_available" | "not_applicable" | "limited_by_budget";
  reason: string;
  checked: number;
  incompatible: number;
  /** Reverse pairings only: the deploy order an incompatibility asks for. */
  order?: string;
}

/** One flag state of a flag-matrix record (Phase 9R item 44); every other flag stays at its default. */
export interface FlagMatrixRow {
  state: string;
  flag: string;
  value: string;
  status: "same" | "changed" | "regressed" | "limited_by_budget" | "not_run";
  reason?: string;
  tests_changed?: number;
}

export interface FlagMatrixBound {
  depth: string;
  max_flags: number;
  touched: number;
  limited_flags: number;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The pairings and deploy-order hints of a compatibility record; empty for other analyses. */
export function compatSummary(summary: Record<string, unknown>): { pairings: CompatPairing[]; deployOrder: string[] } {
  const pairings = Array.isArray(summary.pairings)
    ? summary.pairings.filter((row): row is CompatPairing => isObject(row) && typeof row.pairing === "string" && typeof row.state === "string")
    : [];
  const deployOrder = Array.isArray(summary.deploy_order) ? summary.deploy_order.filter((row): row is string => typeof row === "string") : [];
  return { pairings, deployOrder };
}

/** The on/off rows and bound of a flag-matrix record; null for other analyses. */
export function flagMatrixSummary(summary: Record<string, unknown>): { rows: FlagMatrixRow[]; bound: FlagMatrixBound | null } | null {
  if (!Array.isArray(summary.matrix)) return null;
  const rows = summary.matrix.filter(
    (row): row is FlagMatrixRow => isObject(row) && typeof row.flag === "string" && typeof row.status === "string",
  );
  const bound = isObject(summary.bound) && typeof summary.bound.max_flags === "number" ? (summary.bound as unknown as FlagMatrixBound) : null;
  return { rows, bound };
}

export interface AnalysisVerdictRow {
  stable_key: string;
  status: AnalysisVerdict;
  detail: string;
  evidence_id: string;
  executor_class: string;
  observed_at: string;
}

export interface ArtifactAnalysesProjection {
  revision_id: string;
  historical: boolean;
  run_plan_digest: string;
  base_state: "present" | "absent" | "provisional";
  errors: string[];
  /** Only analyses with something in the trusted base to act on. */
  entries: AnalysisEntryRow[];
  results: AnalysisRecord[];
  verdicts: AnalysisVerdictRow[];
}

export interface AnalysisRunResponse {
  entry_id: string;
  revision_id: string;
  verdict: AnalysisVerdict;
  executions: number;
  reused: number;
  notes: string[];
  executor_class: string;
  run_on: string;
}

export interface AnalysisArtifactView {
  name: string;
  expired: boolean;
  text: string;
  truncated: boolean;
  bytes: number;
}

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/artifact-analyses`;
}

export function fetchArtifactAnalyses(reviewId: string, revisionId = ""): Promise<ArtifactAnalysesProjection> {
  const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
  return requestBrowserJson<ArtifactAnalysesProjection>(`${base(reviewId)}${query}`);
}

/** Execute one planned analysis entry on one exact Snapshot; `pick` adds it to the plan first. */
export function postArtifactAnalysisRun(reviewId: string, revisionId: string, entry: string, pick = false): Promise<AnalysisRunResponse> {
  return requestBrowserJson<AnalysisRunResponse>(`${base(reviewId)}/run`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, entry, pick }),
  });
}

/** One redacted heavy artifact an analysis record cites (bounded server-side). */
export function fetchAnalysisArtifact(reviewId: string, revisionId: string, evidenceId: string, name: string): Promise<AnalysisArtifactView> {
  const query = new URLSearchParams({ revision_id: revisionId, evidence_id: evidenceId, name });
  return requestBrowserJson<AnalysisArtifactView>(`${base(reviewId)}/artifact?${query.toString()}`);
}
