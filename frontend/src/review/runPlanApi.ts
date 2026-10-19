/**
 * Effective run plan (capability catalog + run planner) for one Review Snapshot.
 *
 * The plan is persisted with a digest; executors consume exactly the plan shown
 * here. `Run specific…` can only add entries, and previews cost before adding.
 */
import { evaluateReviewPremergeChecks, postAiReview, postSynthesis, requestBrowserJson } from "./reviewApi";

export type RunPlanEntryStatus =
  | "run"
  | "on_request"
  | "skipped"
  | "inapplicable"
  | "not_available"
  | "limited_by_budget"
  /** An equivalent current-Snapshot result was ingested; the entry is not re-run. */
  | "satisfied";

/** The ingested result a `satisfied` entry relies on (exact Snapshot and head SHA). */
export interface RunPlanSatisfiedBy {
  entry_id: string;
  evidence_id: string;
  check_key: string;
  attestation_class: string;
  status: string;
  head_sha: string;
  equivalence: string;
  title: string;
  observed_at: string;
  url: string;
}

/** The push fast-lane decision; every blocker is named when not eligible. */
export interface RunPlanFastLane {
  eligible: boolean;
  reasons: string[];
  skipped: string[];
}
/** One recipe's test-impact selection (R6 item 19); `reason` always says why. */
export interface RunPlanRecipeSelection {
  recipe_id: string;
  /** `impact`: only `tests` run; `all`: the full suite; `none`: no test reaches the change. */
  mode: "impact" | "all" | "none";
  reason: string;
  total: number;
  tests?: string[];
  input: string;
}

/** A plan entry's per-recipe selection against the trusted base's test-impact map. */
export interface RunPlanSelection {
  map: string;
  recipes: Record<string, RunPlanRecipeSelection>;
}

export interface RunPlanEconomyRow {
  entry_id: string;
  reason: "base_shared" | "carried_forward" | "ci_reuse" | "impact_selection" | string;
  label: string;
  /** `measured`: the reused run's own time; `estimated`: from the plan's entry estimate. */
  basis: "measured" | "estimated";
  minutes: number;
  runs: number;
}

/** Compute saved versus spent for the Review (R6). */
export interface RunPlanEconomy {
  spent_minutes: number;
  saved_minutes: number;
  saved_measured_minutes: number;
  saved_estimated_minutes: number;
  rows: RunPlanEconomyRow[];
}

export type RunPlanEntryOrigin =
  | "preset"
  | "custom"
  | "profile"
  | "policy_floor"
  | "manual"
  | "family_override"
  /** Raised inside the Auto rung by a deterministic risk signal. */
  | "escalation";
export type RunPlanDepth = "quick" | "auto" | "thorough" | "deep";

/** One deterministic signal that escalated (or would have escalated) deep review. */
export interface RunPlanEscalationTrigger {
  class:
    | "criticality"
    | "static_inventory"
    | "changed_contracts"
    | "security_chains"
    | "explicit_policy"
    /** The authoring agent/model's track record (agents only, never a human). */
    | "author_track_record"
    | string;
  label: string;
  paths: string[];
  path_count: number;
  /** Where the threshold that fired comes from (a settings layer or the built-in default). */
  origin: string;
  /** Set on a suppressed trigger: why it did not escalate (deep review disabled for its paths). */
  reason?: string;
}

export type RunPlanEscalationState =
  | "escalated"
  | "none"
  | "disabled"
  | "suppressed"
  | "not_applicable"
  | "limited_by_budget";

/** The plan's deep-review escalation decision; display only, never execution identity. */
export interface RunPlanEscalation {
  state: RunPlanEscalationState;
  to: RunPlanDepth;
  to_origin: string;
  reason: string;
  triggers: RunPlanEscalationTrigger[];
  suppressed: RunPlanEscalationTrigger[];
  ceiling: { max_model_calls: number | null; origin: string };
  entries: string[];
  /** Coverage gaps (e.g. an author track record unknown past its bound); never a trigger. Absent when none. */
  notes?: string[];
}

/** What escalation did to one AI entry. */
export interface RunPlanEntryEscalation {
  state: "escalated" | "limited_by_budget";
  from: string;
  to: string;
  triggers: RunPlanEscalationTrigger[];
}

/** `ai.rule_checks`: one bounded question per rule, within the entry's own ceiling. */
export interface RunPlanRuleChecks {
  ceiling: number;
  ceiling_origin: string;
  planned: number;
  limited: number;
  questions: Array<{
    ref: string;
    stable_key: string;
    digest: string;
    paths: string[];
    path_count: number;
    state: "planned" | "limited_by_budget";
  }>;
}

/** How the effective plan answered a per-Review depth request (a floor is never lowered). */
export interface DepthRequestOutcome {
  entry: string;
  requested: string;
  effective: string | null;
  state: "honored" | "raised" | "limited_by_budget" | "limited" | "refused";
  reason: string;
}

/** Whether AI entries can execute here; deterministic entries never depend on it. */
export interface RunPlanAiAvailability {
  available: boolean;
  reason: string;
  executor: string;
  provider?: string;
}

export interface RunPlanEntry {
  id: string;
  family: string;
  title: string;
  status: RunPlanEntryStatus;
  reason: string;
  origin: RunPlanEntryOrigin;
  when: string;
  sides: string;
  paths: string[];
  executor: string;
  depth: RunPlanDepth;
  required: boolean;
  escalate: boolean;
  cost_class: string;
  degrade_tier: string;
  estimated_minutes: number;
  estimated_model_calls: number;
  pulled_in_by: string[];
  setup_hint: string;
  available: boolean;
  external: boolean;
  budget_reduced_from: string;
  /** Which policy floor made it required: capability, min_effort or check (a typed required check). */
  floor_kind?: string;
  satisfied_by?: RunPlanSatisfiedBy | null;
  /** Test-impact selection per recipe; absent or null: every recipe runs in full. */
  selection?: RunPlanSelection | null;
  /** Head first: the base side runs only to classify a head failure. */
  base_on_demand?: boolean;
  /** Set when deep-review escalation raised (or tried to raise) this entry. */
  escalation?: RunPlanEntryEscalation | null;
  /** `ai.rule_checks` only: the rule questions and the entry's ceiling. */
  rule_checks?: RunPlanRuleChecks | null;
}

export interface RunPlan {
  version: number;
  digest: string;
  parent_digest: string;
  inputs_digest: string;
  review_id: string;
  revision_id: string;
  trigger: string;
  selection: {
    source?: string;
    name?: string;
    base?: string;
    /** Set when a `path_effort` rule chose the starting depth for this Change. */
    path_default?: string;
    path_default_paths?: string;
    path_default_origin?: string;
  };
  run_on: string;
  run_on_note: string;
  budget: {
    ceilings: Record<string, number>;
    usage: Record<string, number | null>;
    planned: { minutes: number; model_calls: number };
    limited: boolean;
    notes: string[];
  };
  family_modes: Record<string, string>;
  entries: RunPlanEntry[];
  manual: Array<{ id: string; depth: RunPlanDepth | null }>;
  /** Selection notes, e.g. a profile whose label rule is inactive (no label source). */
  notes?: string[];
  /** Required work this plan does not run; readiness stays pending on it. */
  required_blockers?: string[];
  /** Push fast lane; null when the Review opened on this Snapshot. */
  fast_lane?: RunPlanFastLane | null;
  /** `invalid` when the trusted-base Review config is invalid: nothing runs. */
  policy_state?: "valid" | "invalid";
  policy_error?: string;
  /** Deep-review escalation decision; null/absent when signals were not observed. */
  escalation?: RunPlanEscalation | null;
  errors: string[];
}

export interface RunPlanComputeRow {
  entry_id: string;
  executor: string;
  stage: string;
  compute_minutes: number;
  runs: number;
}

/** Compute metered in this process for the Review, per catalog entry, executor and stage. */
export interface RunPlanCompute {
  rows: RunPlanComputeRow[];
  totals: { compute_minutes: number; runs: number };
  source: string;
}

export interface RunPlanSettingOrigin {
  layer: string;
  source: string;
}

export interface RunPlanSetting<T> {
  effective_value: T;
  mode: "inherit" | "override";
  origin: RunPlanSettingOrigin;
}

export interface RunPlanCatalogRow {
  id: string;
  family: string;
  title: string;
  cost_class: string;
  available: boolean;
  external: boolean;
}

export interface RunPlanProjection {
  plan: RunPlan;
  settings: {
    effort: RunPlanSetting<string>;
    run_on: RunPlanSetting<string>;
    errors: string[];
  };
  catalog: RunPlanCatalogRow[];
  future_settings: { changed: boolean; applies_to: string; errors: string[] };
  compute?: RunPlanCompute;
  /** Compute saved (shared base, carried forward, CI reuse, impact selection) versus spent. */
  economy?: RunPlanEconomy;
  /** Trusted-base `ci_equivalents`: which typed results may stand in for which entries. */
  ci_equivalents?: { rows: Array<Record<string, string>>; errors: string[] };
  revision_id: string;
  historical: boolean;
  ai_availability?: RunPlanAiAvailability;
}

export interface RunSpecificPreview {
  changes: RunPlanEntry[];
  incremental: { minutes: number; model_calls: number };
  limited_by_budget: string[];
  rejected: string[];
  plan: RunPlan;
}

export interface RunSpecificNext {
  entry: string;
  action: "ai_review" | "synthesis" | "premerge_checks" | "runtime_validation" | "check_recipes" | "external_evidence" | "executor";
  depth: RunPlanDepth;
}

export interface RunSpecificResponse {
  confirmed: boolean;
  preview: RunSpecificPreview;
  plan?: RunPlan;
  next?: RunSpecificNext[];
  /** One per pick that named a depth: raised to a floor, limited by budget, or honored. */
  depth_requests?: DepthRequestOutcome[];
}

export interface RunSpecificPick {
  id: string;
  depth?: RunPlanDepth;
}

export async function fetchRunPlan(reviewId: string, revisionId = ""): Promise<RunPlanProjection> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return requestBrowserJson<RunPlanProjection>(`/api/reviews/${encodeURIComponent(reviewId)}/run-plan${query}`);
}

export async function postRunSpecific(
  reviewId: string,
  expectedRevisionId: string,
  entries: RunSpecificPick[],
  confirm: boolean,
): Promise<RunSpecificResponse> {
  return requestBrowserJson<RunSpecificResponse>(`/api/reviews/${encodeURIComponent(reviewId)}/run-plan/run-specific`, {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId, entries, confirm }),
  });
}

/** Start the follow-up work a confirmed pick added, through the existing executors. */
export async function startRunSpecific(
  reviewId: string,
  revisionId: string,
  next: RunSpecificNext[],
): Promise<Record<string, string>> {
  const outcome: Record<string, string> = {};
  const ai = next.filter((row) => row.action === "ai_review");
  if (ai.length > 0) {
    const order: RunPlanDepth[] = ["quick", "auto", "thorough", "deep"];
    const depth = ai.map((row) => row.depth).reduce((a, b) => (order.indexOf(b) > order.indexOf(a) ? b : a));
    try {
      const started = await postAiReview(reviewId, revisionId, depth);
      // A request below a floor is raised, never refused silently: say why.
      const request = started.ai_review?.depth_request;
      const note = request && request.state !== "honored" && request.reason ? ` · ${request.reason}` : "";
      for (const row of ai) outcome[row.entry] = `started${note}`;
    } catch (error) {
      for (const row of ai) outcome[row.entry] = error instanceof Error ? error.message : "could not start";
    }
  }
  if (next.some((row) => row.action === "synthesis")) {
    try {
      await postSynthesis(reviewId, revisionId);
      outcome["ai.brief_synthesis"] = "started";
    } catch (error) {
      outcome["ai.brief_synthesis"] = error instanceof Error ? error.message : "could not start";
    }
  }
  const premerge = next.find((row) => row.action === "premerge_checks");
  if (premerge) {
    try {
      await evaluateReviewPremergeChecks(reviewId, { expected_revision_id: revisionId });
      outcome[premerge.entry] = "started";
    } catch (error) {
      outcome[premerge.entry] = error instanceof Error ? error.message : "could not start";
    }
  }
  for (const row of next) {
    if (outcome[row.entry]) continue;
    if (row.action === "runtime_validation") outcome[row.entry] = "planned; start it from Surfaces";
    else if (row.action === "check_recipes") outcome[row.entry] = "planned; run it from Check recipes";
    else if (row.action === "external_evidence") outcome[row.entry] = "planned; attach its browser evidence";
    else outcome[row.entry] = "planned; runs when its executor is available";
  }
  return outcome;
}
