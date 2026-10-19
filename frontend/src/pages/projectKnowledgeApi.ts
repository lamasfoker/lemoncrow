/**
 * The Project Knowledge surface of the local Review service (UX0b).
 *
 * Reads never call a model. Hide supersedes one AI narrative; regenerate queues
 * one background job; a correction comes back as a patch and nothing is written.
 */
import { requestBrowserJson } from "../review/reviewApi";

export const PROJECT_KNOWLEDGE_PATH = "/api/project-knowledge";

export interface KnowledgeSource {
  provider: string;
  repository_key: string;
  canonical_ref: string;
  repo_root: string;
  updated_at: string;
}

export interface KnowledgeRepositoryRow {
  repository_id: string;
  canonical_ref: string;
  current_generation: string;
  updated_at: string;
  entries: { deterministic: number; source_document: number; ai_narrated: number };
  source: KnowledgeSource | null;
}

export interface KnowledgeListing {
  available: boolean;
  scope: "local";
  repositories: KnowledgeRepositoryRow[];
  truncated: boolean;
  sync_command: string;
  legacy_overlay_note: string;
}

export interface KnowledgeEvent {
  kind: string;
  label: string;
  generation_id: string;
  observed_at: string;
}

export interface KnowledgeEntry {
  entry_id: string;
  subject: { kind: string; key: string };
  knowledge_kind: string;
  narrative: "subject" | "change" | null;
  body: string;
  body_truncated: boolean;
  state: string;
  support: "deterministic" | "source_document" | "ai_narrated";
  support_label: string;
  source_refs: string[];
  source_refs_omitted: number;
  source_paths: string[];
  source_paths_omitted: number;
  review_refs: string[];
  merge_refs: string[];
  generation: string;
  event: KnowledgeEvent | null;
  updated_at: string;
}

export interface KnowledgeOverview {
  repository: { repository_id: string; current_generation: string; source: KnowledgeSource | null };
  narratives: KnowledgeEntry[];
  narratives_truncated: boolean;
  context: { entries: KnowledgeEntry[]; term: string; offset: number; next_offset: number | null };
  evolution: KnowledgeEvent[];
  regenerate: { available: boolean; reason: string };
}

export interface HideResult {
  status: "hidden" | "already_hidden";
  entry_id: string;
}

export interface RegenerateResult {
  status: "queued" | "refused";
  reason?: string;
  entry_id: string;
  job_id?: string;
  enqueued?: boolean;
}

export interface CorrectionProposal {
  path: string;
  content: string;
  patch: string;
}

function repositoryPath(repositoryId: string): string {
  return `${PROJECT_KNOWLEDGE_PATH}/${encodeURIComponent(repositoryId)}`;
}

export function fetchKnowledgeListing(): Promise<KnowledgeListing> {
  return requestBrowserJson<KnowledgeListing>(PROJECT_KNOWLEDGE_PATH);
}

export function fetchKnowledgeOverview(
  repositoryId: string,
  { term = "", offset = 0 }: { term?: string; offset?: number } = {},
): Promise<KnowledgeOverview> {
  const params = new URLSearchParams();
  if (term) params.set("q", term);
  if (offset) params.set("offset", String(offset));
  const query = params.toString();
  return requestBrowserJson<KnowledgeOverview>(`${repositoryPath(repositoryId)}${query ? `?${query}` : ""}`);
}

function narrativePath(repositoryId: string, entryId: string, action: "hide" | "regenerate"): string {
  return `${repositoryPath(repositoryId)}/narratives/${encodeURIComponent(entryId)}/${action}`;
}

export function hideNarrative(repositoryId: string, entryId: string): Promise<HideResult> {
  return requestBrowserJson<HideResult>(narrativePath(repositoryId, entryId, "hide"), { method: "POST" });
}

export function regenerateNarrative(repositoryId: string, entryId: string): Promise<RegenerateResult> {
  return requestBrowserJson<RegenerateResult>(narrativePath(repositoryId, entryId, "regenerate"), {
    method: "POST",
  });
}

export function proposeCorrection(
  repositoryId: string,
  body: { text: string; entry_id: string },
): Promise<CorrectionProposal> {
  return requestBrowserJson<CorrectionProposal>(`${repositoryPath(repositoryId)}/corrections`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
