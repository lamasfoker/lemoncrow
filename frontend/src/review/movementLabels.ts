import type { ReviewMovementSummary } from "./types";

/**
 * The "changed since" summary's base-only note (Phase 9 item 34).
 *
 * Only deterministic base-only movement is labelled; anything undecidable was
 * already classified as changed by the server and is never softened here.
 */
export function baseOnlyNote(movement?: ReviewMovementSummary | null): string {
  if (!movement || !movement.base_moved) return "";
  const files = movement.base_only_paths.length;
  if (files === 0) return "";
  const lead =
    movement.kind === "rebase_only" || movement.kind === "base_only"
      ? "Rebase only"
      : "Base moved";
  return `${lead} · ${files} file${files === 1 ? "" : "s"} base-only, judgments kept`;
}
