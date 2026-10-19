import type { ReviewProjection, ReviewProjectionItem, ReviewTarget } from "./types";

export type ReviewPersistentView = "brief" | "implementation" | "map";
export type ReviewLandingView = "brief" | "implementation";

export interface ReviewCohort {
  /** Stable conceptual identity shared by Brief, Guided Review, Code and Map. */
  id: string;
  itemId: string;
  title: string;
  kind: ReviewProjectionItem["kind"];
  attention: ReviewProjectionItem["attention"];
  position: number | null;
  targetIds: string[];
  primaryTargetIds: string[];
  paths: string[];
}
export const LARGE_REVIEW_TARGET_THRESHOLD = 400;

const MATERIAL_SIGNALS = new Set([
  "security_trust_boundary",
  "state_persistence",
  "public_contract",
]);

function unique(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

export function reviewCohorts(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
): ReviewCohort[] {
  if (!projection) return [];
  const byId = new Map(targets.map((target) => [target.target_id, target]));
  return projection.items
    .map((item) => {
      const id = item.key || item.id;
      const targetIds = unique([...item.primary_target_ids, ...item.related_target_ids]);
      const paths = unique(targetIds.map((targetId) => byId.get(targetId)?.path ?? ""));
      return {
        id,
        itemId: item.id,
        title: item.title,
        kind: item.kind,
        attention: item.attention,
        position: item.position,
        targetIds,
        primaryTargetIds: unique(item.primary_target_ids),
        paths,
      };
    })
    .filter((cohort) => cohort.id && cohort.targetIds.length > 0);
}

export function projectionSearchTextByTarget(
  projection: ReviewProjection | null,
): Map<string, string> {
  const out = new Map<string, string>();
  if (!projection) return out;
  const append = (targetIds: readonly string[], text: string) => {
    const clean = text.trim();
    if (!clean) return;
    for (const targetId of targetIds) {
      out.set(targetId, [out.get(targetId) ?? "", clean].filter(Boolean).join(" "));
    }
  };
  for (const item of projection.items) {
    append(
      [...item.primary_target_ids, ...item.related_target_ids],
      [item.title, item.before, item.after, item.rationale, ...item.concerns.map((row) => row.scope + " " + row.reason)].join(" "),
    );
  }
  for (const step of projection.tour?.steps ?? []) {
    append(step.target_ids, [step.title, step.why, ...step.signals].join(" "));
  }
  for (const statement of projection.summary?.statements ?? []) {
    append(statement.target_ids, statement.text);
  }
  for (const claim of projection.claims) append(claim.target_ids, [claim.statement, claim.summary_text].join(" "));
  return out;
}

export function cohortForTarget(
  cohorts: readonly ReviewCohort[],
  targetId: string | null | undefined,
): ReviewCohort | null {
  if (!targetId) return null;
  return cohorts.find((cohort) => cohort.targetIds.includes(targetId)) ?? null;
}

export function isTinyMechanicalReview(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
): boolean {
  if (!projection || targets.length === 0) return false;
  const cohorts = reviewCohorts(projection, targets);
  const allMechanical = cohorts.length === 0
    ? targets.every((target) => target.attention_level === "mechanical")
    : cohorts.every((cohort) => cohort.kind === "mechanical");
  const verificationConcern = targets.some((target) => target.verification.fail > 0 || target.verification.unknown > 0);
  const humanDecision = projection.decisions.some((decision) => !decision.state || decision.state.outstanding)
    || projection.actions.some((action) => action.id.startsWith("decision:"))
    || projection.questions.length > 0;
  // An observed behavior change is never "tiny and mechanical".
  const behaviorChange = (projection.behavior?.total ?? 0) > 0;
  return (
    projection.coverage.file_count <= 2
    && projection.coverage.target_count <= 3
    && allMechanical
    && !verificationConcern
    && !humanDecision
    && !behaviorChange
  );
}

export function guidedReviewUseful(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
): boolean {
  if (!projection?.tour?.step_count || isTinyMechanicalReview(projection, targets)) return false;
  return projection.tour.steps.some((step) => step.target_ids.length > 0);
}

export function reviewMapUseful(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
): boolean {
  if (!projection || isTinyMechanicalReview(projection, targets)) return false;
  const cohorts = reviewCohorts(projection, targets);
  const materialSignal = projection.tour?.steps.some((step) =>
    step.signals.some((signal) => MATERIAL_SIGNALS.has(signal)),
  ) ?? false;
  const multiConcern = cohorts.length >= 2 && projection.coverage.file_count >= 4;
  return materialSignal || multiConcern;
}

export function persistentReviewViews(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
  briefCapable: boolean,
): ReviewPersistentView[] {
  if (!briefCapable || !projection) return ["implementation"];
  if (isTinyMechanicalReview(projection, targets)) return ["implementation"];
  return [
    "brief",
    "implementation",
    ...(reviewMapUseful(projection, targets) ? ["map" as const] : []),
  ];
}

export function adaptiveReviewLanding(
  projection: ReviewProjection | null,
  targets: readonly ReviewTarget[],
  briefCapable: boolean,
): ReviewLandingView {
  if (!briefCapable || !projection || isTinyMechanicalReview(projection, targets)) return "implementation";
  return "brief";
}
