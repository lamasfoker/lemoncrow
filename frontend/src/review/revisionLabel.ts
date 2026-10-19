import type { RevisionInfo } from "./types";

export function revisionLabel(revision: Pick<RevisionInfo, "capture_kind" | "commit_number" | "commit_sha" | "commit_state" | "draft_number" | "revision_number" | "is_published_snapshot">): string {
  if (revision.capture_kind === "draft" && revision.is_published_snapshot === false) return `Draft capture ${revision.revision_number}`;
  const snapshot = `Snapshot ${revision.revision_number}`;
  if (revision.capture_kind === "commit") {
    const number = revision.commit_number != null ? `Commit ${revision.commit_number}` : "Commit";
    const sha = revision.commit_sha?.slice(0, 8);
    const label = sha ? `${snapshot} · ${number} · ${sha}` : `${snapshot} · ${number}`;
    return revision.commit_state === "superseded" ? `${label} · rewritten` : label;
  }
  return snapshot;
}

export function revisionShortLabel(revision: Pick<RevisionInfo, "capture_kind" | "commit_number" | "commit_state" | "draft_number" | "revision_number" | "is_published_snapshot">): string {
  if (revision.capture_kind === "draft" && revision.is_published_snapshot === false) return "Running draft";
  return `Snapshot ${revision.revision_number}`;
}
