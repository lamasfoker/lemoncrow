import { requestBrowserJson } from "./reviewApi";

/** How one predicted behavior target fared in production (P15-OBS). */
export type ProductionTargetState = "confirmed" | "contradicted" | "not_observed";

export interface ProductionTargetOutcome {
  key: string;
  kind: "route" | "outbound_host" | "unobservable";
  subject: string;
  change: string;
  title: string;
  state: ProductionTargetState;
  reason: string;
  signals: string[];
}

export interface ProductionObservationView {
  environment: string;
  deployment_id: string;
  provider: string;
  merged_commit: string;
  window: { start: string; end: string; baseline_start: string };
  state: "observing" | "closed";
  close_reason: string;
  counts: Partial<Record<ProductionTargetState, number>>;
  outcomes: ProductionTargetOutcome[];
  predictions_dropped: number;
  budget: number;
  spent: number;
  queries_planned: number;
  queries_answered: number | null;
}

/** An incident that may be related to this Change: advisory correlation with its reasons, never blame. */
export interface ProductionIncidentLink {
  incident: { source: string; id: string; name: string; service: string; route: string; started_at: string };
  review_id: string;
  environment: string;
  deployment_id: string;
  reasons: string[];
  reason_labels: string[];
  summary: string;
  linked_at: string;
  advisory: true;
}

export interface ProductionObservabilityView {
  configured: boolean;
  locus?: string;
  connectors?: Array<{ id: string; kind: string }>;
  status_line?: string;
  observations?: ProductionObservationView[];
  incidents?: ProductionIncidentLink[];
  incidents_total?: number;
}

/** A current production problem: a failed or rolled-back deployment, a contradiction, or an advisory incident. */
export interface ProductionProblem {
  id: string;
  kind: "deployment_failed" | "deployment_rolled_back" | "contradicted" | "incident";
  title: string;
  detail: string;
  revision_id: string;
  environment: string;
  observed_at: string;
  advisory: boolean;
}

export type ProductionLinkRelation = "reverted_by" | "reverts" | "fixed_forward_by" | "fixes_forward";

export interface ProductionChangeLink {
  relation: ProductionLinkRelation;
  target_review_id: string;
  title: string;
  created_at: string;
}

/** ``configured: false`` is the whole answer without a deployment or observability connector. */
export interface ReviewProductionView {
  configured: boolean;
  observability?: ProductionObservabilityView;
  problems?: ProductionProblem[];
  links?: ProductionChangeLink[];
  rollback?: { decision: { mode: string; reason: string; rule_id: string }; adapter: string; automatic: boolean; reason: string };
  freshness_class?: "external_observation";
  in_readiness?: false;
}

export function fetchReviewProduction(reviewId: string): Promise<ReviewProductionView> {
  return requestBrowserJson<ReviewProductionView>(`/api/reviews/${encodeURIComponent(reviewId)}/production`);
}
