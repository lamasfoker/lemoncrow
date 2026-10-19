import { requestBrowserJson } from "./reviewApi";
import type { ReviewTarget } from "./types";

/** Local editor launch contract shared with the /code workspace (`/v1/code-map/editor`). */
export interface ReviewEditorCapability {
  available: boolean;
  preferred: { id: string; label: string } | null;
}

export interface ReviewEditorLocation {
  path: string;
  line: number;
}

const EDITOR_PATH = "/api/v1/code-map/editor";

export async function fetchReviewEditorCapability(): Promise<ReviewEditorCapability> {
  return await requestBrowserJson<ReviewEditorCapability>(EDITOR_PATH);
}

export async function openReviewTargetInEditor(repoRoot: string, location: ReviewEditorLocation): Promise<unknown> {
  return await requestBrowserJson<unknown>(EDITOR_PATH, {
    method: "POST",
    body: JSON.stringify({ project_root: repoRoot, path: location.path, line: location.line, column: 1 }),
  });
}

/**
 * Where the current workspace file for a target starts. A target whose change
 * only exists on the old side (a deletion) has no workspace file to open.
 */
export function reviewEditorLocation(target: ReviewTarget | null | undefined): ReviewEditorLocation | null {
  if (!target?.path) return null;
  const newSpans = target.spans.filter((span) => span.side === "new");
  const oldSpans = target.spans.filter((span) => span.side === "old");
  if (newSpans.length === 0 && oldSpans.length > 0 && target.start_line <= 0) return null;
  const line = newSpans.length > 0
    ? Math.min(...newSpans.map((span) => span.start_line))
    : target.start_line;
  return { path: target.path, line: Math.max(1, line || 1) };
}
