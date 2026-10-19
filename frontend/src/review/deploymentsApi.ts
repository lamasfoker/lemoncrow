import { requestBrowserJson } from "./reviewApi";

/** Where the merged Change stands in one environment (P15-DEPLOY). */
export type DeploymentOutcomeState = "deployed" | "deploying" | "failed" | "rolled_back" | "superseded" | "unknown";
/** How the deciding deployment's commit relates to the merged commit. */
export type DeploymentRelation = "exact" | "descendant" | "not_contained" | "unknown";

export interface DeploymentConnectorView {
  connector: string;
  state: "configured" | "not_configured";
  locus: string;
  reason: string;
}

export interface MergedCommitView {
  provider: string;
  sha: string;
  merged_at: string;
  ref: string;
}

export interface DeploymentEnvironmentView {
  environment: string;
  key: string;
  state: DeploymentOutcomeState;
  label: string;
  deciding_id: string;
  commit: string;
  relation: DeploymentRelation;
  since: string;
  open_until: string;
  production: boolean;
  traffic_percent: number | null;
  url: string;
  provider: string;
  entries_dropped: number;
}

/** ``configured: false`` carries only the connector status: nothing else is shown. */
export interface ReviewDeploymentsView {
  configured: boolean;
  connector: DeploymentConnectorView;
  merged?: MergedCommitView | null;
  status_line?: string;
  environments?: DeploymentEnvironmentView[];
  environments_dropped?: number;
  freshness_class?: "external_observation";
  in_readiness?: false;
}

export function fetchReviewDeployments(reviewId: string): Promise<ReviewDeploymentsView> {
  return requestBrowserJson<ReviewDeploymentsView>(`/api/reviews/${encodeURIComponent(reviewId)}/deployments`);
}
