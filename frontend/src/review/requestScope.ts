import type { ReviewRequestScope, ReviewTarget } from "./types";

/**
 * Target ids inside a scoped request (the hosted `in_scope` key rule: a `paths`
 * scope names target paths, a `targets` scope names unit keys); `null` when
 * there is no scope to follow.
 */
export function requestScopeTargetIds(
  targets: readonly ReviewTarget[],
  scope: ReviewRequestScope | null | undefined,
): ReadonlySet<string> | null {
  if (!scope || scope.values.length === 0) return null;
  const allowed = new Set(scope.values);
  return new Set(
    targets
      .filter((target) => allowed.has(scope.kind === "paths" ? target.path : target.unit_key))
      .map((target) => target.target_id),
  );
}
