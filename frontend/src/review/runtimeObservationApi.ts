/**
 * Runtime side effects and deep rendering diffs for one exact Review Snapshot (Phase 9R R7).
 *
 * Every run is admitted by the effective run plan, sandboxed, metered and
 * compared base vs head under one environment fingerprint; a pair that is not
 * comparable yields no delta, never a regression.
 */
import { authenticatedFetch, requestBrowserJson } from "./reviewApi";

export type ObservationCategory = "network" | "file_write" | "process" | "db_query" | "schema_mutation" | "console_error";

export interface ObservedEffect {
  category: ObservationCategory;
  identity: string;
  detail: string;
  count: number;
  outcome: "blocked" | "intercepted" | "connected" | "";
  sources: string[];
}

export interface EffectChange {
  category: ObservationCategory;
  identity: string;
  change: "added" | "removed" | "changed";
  base: ObservedEffect | null;
  head: ObservedEffect | null;
}

export interface ObservationSide {
  status: string;
  reason: string;
  evidence_id: string;
  evidence_ref: string;
  environment: string;
  observation: { tier: string; truncated: boolean; query_count: number; counts: Record<string, number> } | null;
}

export interface SideEffectResult {
  kind: "diff";
  recipe: { id: string; title: string; digest: string };
  executor_class: string;
  environment: Record<string, string>;
  sides: Partial<Record<"base" | "head", ObservationSide>>;
  diff: {
    comparable: boolean;
    reasons: string[];
    differences: string[];
    changes: EffectChange[];
    unchanged: number;
    query_count?: { base: number; head: number };
  };
  evidence_id: string;
  observed_at: string;
}

export type RenderAspect = "screenshot" | "dom" | "a11y" | "network" | "console" | "layout_shift" | "performance" | "status";

export interface RenderPageDelta {
  cell: { id: string; route: string; theme: string; flow: string; viewport: { id: string; width: number; height: number } };
  changes: Partial<Record<RenderAspect, Record<string, unknown>>>;
}

export interface RenderResult {
  kind: "render_diff";
  entry: string;
  executor_class: string;
  selection: { routes: string[]; mapping: "known" | "unknown" | "none"; note: string; unmapped: string[] };
  plan: { cells: Array<{ id: string }>; dropped: string[]; limited_by_depth: boolean; limited_by_budget: boolean; estimate_seconds: number };
  browser: string;
  comparable: boolean;
  differences: string[];
  reasons: string[];
  pages: RenderPageDelta[];
  unchanged: number;
  evidence_id: string;
  observed_at: string;
}

export interface ObservationEntryRow {
  id: string;
  status: string;
  reason: string;
  sides: string;
  executor: string;
  required: boolean;
  estimated_minutes: number;
}

export interface RuntimeObservationProjection {
  revision_id: string;
  historical: boolean;
  run_plan_digest: string;
  entries: ObservationEntryRow[];
  side_effects: SideEffectResult[];
  render: RenderResult[];
  limitations: string[];
}

export interface RuntimeObservationRunResponse {
  entry_id: string;
  revision_id: string;
  executions: number;
  reused: number;
  notes: string[];
  limited_by_budget?: boolean;
  executor_class: string;
  run_on: string;
}

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/runtime-observation`;
}

export function fetchRuntimeObservation(reviewId: string, revisionId = ""): Promise<RuntimeObservationProjection> {
  const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
  return requestBrowserJson<RuntimeObservationProjection>(`${base(reviewId)}${query}`);
}

/** Execute one planned observation entry on one exact Snapshot; `pick` adds it to the plan first. */
export function postRuntimeObservationRun(
  reviewId: string,
  revisionId: string,
  entry: string,
  pick = false,
): Promise<RuntimeObservationRunResponse> {
  return requestBrowserJson<RuntimeObservationRunResponse>(`${base(reviewId)}/run`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, entry, pick }),
  });
}

/** One referenced run artifact (masked screenshot, DOM/AX snapshot); 410 once raw retention lapsed. */
export async function fetchRuntimeArtifact(reviewId: string, ref: string, revisionId = ""): Promise<Blob> {
  const digest = ref.slice(ref.lastIndexOf(":") + 1);
  const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
  const response = await authenticatedFetch(`${base(reviewId)}/artifacts/${encodeURIComponent(digest)}${query}`);
  if (response.status === 410) throw new Error("expired under raw retention; re-run to observe it again");
  if (!response.ok) throw new Error(`${response.status}`);
  return response.blob();
}
