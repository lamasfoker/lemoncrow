-- PK0 Project Knowledge: repository-scoped, rebuildable derived state.
-- This is deliberately not Review/evidence/policy authority. No foreign key to a
-- Review aggregate: landed repository history outlives any one Review and may be
-- deleted/rebuilt independently from authoritative source/Review/outcome stores.
CREATE TABLE IF NOT EXISTS project_knowledge_meta (
  repository_id      TEXT PRIMARY KEY,
  current_generation TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_knowledge_entries (
  entry_id                 TEXT PRIMARY KEY,
  repository_id            TEXT NOT NULL,
  canonical_ref_id         TEXT NOT NULL,
  canonical_ref            TEXT NOT NULL,
  subject_kind             TEXT NOT NULL,
  subject_key              TEXT NOT NULL,
  knowledge_kind           TEXT NOT NULL,
  body                     TEXT NOT NULL,
  valid_from_generation    TEXT NOT NULL,
  last_observed_generation TEXT NOT NULL,
  state                    TEXT NOT NULL,
  support                  TEXT NOT NULL,
  source_refs_json         TEXT NOT NULL,
  review_refs_json         TEXT NOT NULL,
  revision_refs_json       TEXT NOT NULL,
  merge_refs_json          TEXT NOT NULL,
  source_paths_json        TEXT NOT NULL,
  content_access_class     TEXT NOT NULL,
  synthesis_identity       TEXT NOT NULL,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_knowledge_query
  ON project_knowledge_entries(
    repository_id, subject_kind, subject_key, knowledge_kind, state, updated_at DESC
  );

CREATE TABLE IF NOT EXISTS project_knowledge_events (
  event_id               TEXT PRIMARY KEY,
  repository_id          TEXT NOT NULL,
  kind                   TEXT NOT NULL,
  generation_id          TEXT NOT NULL,
  previous_generation_id TEXT NOT NULL,
  source_refs_json       TEXT NOT NULL,
  subject_refs_json      TEXT NOT NULL,
  observed_at            TEXT NOT NULL,
  payload_digest         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_knowledge_events_repo
  ON project_knowledge_events(repository_id, observed_at DESC, event_id);
