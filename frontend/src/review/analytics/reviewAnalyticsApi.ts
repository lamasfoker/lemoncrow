import { requestBrowserJson } from "../reviewApi";

/* -- Review analytics (P13-ANALYTICS): advisory, read-only, every figure traceable to its Reviews -- */

/** One aggregate: how many records it counted, in how many Reviews, and the key that lists them. */
export interface AnalyticsRow {
  key: string;
  label: string;
  count: number;
  reviews: number;
  drill: string;
  /** Present when the aggregate is a sum (hours, minutes, model calls, money). */
  value?: number;
  median_hours?: number | null;
  max_hours?: number | null;
  share?: number | null;
  cost_class?: string;
  locus?: string;
  currency?: string;
  executor?: string;
}

export interface AnalyticsScopeWire {
  org: string;
  repos: string[];
  teams: string[];
  paths: string[];
  since: string;
  until: string;
}

export interface RuleRow {
  rule: string;
  kind: string;
  tool: string;
  check: string;
  asked: number;
  hits: number;
  acted_on: number;
  dismissed: number;
  false_positive: number;
  hit_rate: number | null;
  dismissal_rate: number | null;
  acted_on_rate: number | null;
  signal: "signal" | "noise" | "insufficient_data";
  drill: Record<string, string>;
}

export interface CheckRow {
  check: string;
  kind: string;
  hits: number;
  acted_on: number;
  dismissed: number;
  dismissal_rate: number | null;
  acted_on_rate: number | null;
  signal: "signal" | "noise" | "insufficient_data";
  drill: Record<string, string>;
}

export interface EscalationLane {
  reviews: AnalyticsRow;
  useful_ai_findings: AnalyticsRow;
  ai_model_calls: AnalyticsRow;
  useful_per_review: number | null;
  model_calls_per_useful: number | null;
}

export interface CostPerUseful {
  measure: "provider_spend" | "model_calls" | "compute_minutes" | "job_minutes";
  cost_class: string;
  currency?: string;
  per_useful_finding: number | null;
  numerator_drill: string | string[];
  denominator_drill: string;
}

export interface DriftItem {
  review_id: string;
  repo_id: string;
  repo_name: string;
  review_title: string;
  review_path: string;
  revision_id: string;
  merged: boolean;
  at: string;
  category: string;
  change: string;
  title: string;
  critical: boolean;
  judgment: { state: string; label: string; by: string; at: string; event_id: string };
}

export interface ReviewAnalyticsReport {
  version: number;
  gating: "never";
  individuals: "not_reported";
  generated_at: string;
  scope: AnalyticsScopeWire;
  teams_supported: boolean;
  bounds: {
    max_reviews: number;
    candidates: number;
    analyzed: number;
    in_scope: number;
    truncated: boolean;
    scan_truncated: boolean;
    escapes_truncated: boolean;
  };
  notes: string[];
  /** Optional sources this engine does not keep (e.g. the Review-only local engine), with the sections they feed. */
  unavailable?: { source: string; label: string; sections: string[] }[];
  volume: {
    reviews: AnalyticsRow;
    by_status: AnalyticsRow[];
    by_author_kind: AnalyticsRow[];
    by_repository: AnalyticsRow[];
  };
  time_to_first_review: {
    measured: AnalyticsRow;
    awaiting: AnalyticsRow;
    median_hours: number | null;
    p90_hours: number | null;
  };
  merge_time: { merged: AnalyticsRow; median_hours: number | null; p90_hours: number | null };
  findings: {
    current: AnalyticsRow;
    by_source: AnalyticsRow[];
    by_category: AnalyticsRow[];
    by_severity: AnalyticsRow[];
    by_outcome: AnalyticsRow[];
    invalidated: AnalyticsRow[];
    basis: string;
  };
  catch_contribution: {
    useful: AnalyticsRow;
    deterministic: AnalyticsRow;
    ai: AnalyticsRow;
    acted_on_rate: number | null;
  };
  rules: RuleRow[];
  checks: CheckRow[];
  policy_effectiveness: {
    min_judged: number;
    signal_rate: number;
    signal: string[];
    noise: string[];
    checks_signal: string[];
    checks_noise: string[];
    basis: string;
  };
  auto_approval: {
    evaluated: AnalyticsRow;
    eligible: AnalyticsRow;
    not_eligible: AnalyticsRow;
    failed_gates: (AnalyticsRow & {
      gate: string;
      reason: string;
      policy_generations: string[];
      policy_generation_count: number;
    })[];
  };
  overrides: { break_glass: AnalyticsRow; bypassed: AnalyticsRow[] };
  depth: { distribution: AnalyticsRow[] };
  escalation: { escalated: EscalationLane; standard: EscalationLane; triggers: AnalyticsRow[] };
  cost: {
    model_calls: AnalyticsRow[];
    provider_spend: AnalyticsRow[];
    calls_without_cost: AnalyticsRow[];
    compute_minutes: AnalyticsRow[];
    job_minutes: AnalyticsRow[];
    basis: string;
  };
  cache_reuse: AnalyticsRow[];
  cost_per_useful_finding: CostPerUseful[];
  correction_loops: Record<
    "started" | "snapshot_recorded" | "re_reviewed" | "in_flight" | "failed" | "cancelled",
    AnalyticsRow
  > & { completion_rate: number | null };
  escaped_defects: {
    total: AnalyticsRow;
    by_depth: AnalyticsRow[];
    by_check: AnalyticsRow[];
    by_kind: AnalyticsRow[];
    basis: string;
  };
  drift: {
    granularity: "week" | "month";
    changes: AnalyticsRow;
    by_judgment: AnalyticsRow[];
    by_category: AnalyticsRow[];
    by_change: AnalyticsRow[];
    series: AnalyticsRow[];
    items: DriftItem[];
    items_total: number;
  };
  waiting_by_owner: AnalyticsRow[];
  reviewer_load: {
    waiting_on_reviewers?: AnalyticsRow;
    by_team?: AnalyticsRow[];
    teams_shown?: "all" | "yours";
    mine?: { to_review: AnalyticsRow; waiting_on_me: AnalyticsRow };
    individuals?: "not_reported";
    basis?: string;
  };
}

export interface DrillRecord {
  review_id: string;
  repo_id: string;
  record_kind: string;
  record_id: string;
  revision_id: string;
  at: string;
  label: string;
  value: number;
  review_title: string;
  repo_name: string;
  review_path: string;
}

export interface ReviewAnalyticsDrill {
  key: string;
  label: string;
  known: boolean;
  count: number;
  value: number;
  reviews: number;
  records: DrillRecord[];
  limit: number;
  truncated: boolean;
  scope: AnalyticsScopeWire;
}

export interface ReviewAnalyticsQuery {
  days?: number;
  since?: string;
  until?: string;
  repos?: string[];
  teams?: string[];
  paths?: string[];
}

/** Hosted analytics sit beside the Inbox; the local server serves them under the browser's /api. */
export function reviewAnalyticsPath(mode: string): string {
  return mode === "hosted" || mode === "hybrid" ? "/v1/reviews/analytics" : "/api/review-analytics";
}

export function reviewAnalyticsParams(query: ReviewAnalyticsQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.since) params.set("since", query.since);
  if (query.until) params.set("until", query.until);
  if (!query.since && query.days) params.set("days", String(query.days));
  for (const repo of query.repos ?? []) params.append("repo", repo);
  for (const team of query.teams ?? []) params.append("team", team);
  for (const path of query.paths ?? []) params.append("path", path);
  return params;
}

export function fetchReviewAnalytics(mode: string, query: ReviewAnalyticsQuery): Promise<ReviewAnalyticsReport> {
  return requestBrowserJson<ReviewAnalyticsReport>(
    `${reviewAnalyticsPath(mode)}?${reviewAnalyticsParams(query).toString()}`,
  );
}

/** The records behind one figure, over exactly the window the report was computed for. */
export function fetchReviewAnalyticsDrill(
  mode: string,
  key: string,
  scope: AnalyticsScopeWire,
): Promise<ReviewAnalyticsDrill> {
  const params = reviewAnalyticsParams({
    since: scope.since,
    until: scope.until,
    repos: scope.repos,
    teams: scope.teams,
    paths: scope.paths,
  });
  params.set("key", key);
  return requestBrowserJson<ReviewAnalyticsDrill>(`${reviewAnalyticsPath(mode)}/drill?${params.toString()}`);
}
