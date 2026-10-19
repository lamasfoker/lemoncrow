DROP INDEX IF EXISTS ux_review_revisions_tree;
CREATE INDEX IF NOT EXISTS idx_review_revisions_tree
  ON review_revisions(review_id, tree_fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_revisions_snapshot
  ON review_revisions(review_id, tree_fingerprint, source_fingerprint);

CREATE TABLE IF NOT EXISTS review_relationships (
  id               TEXT PRIMARY KEY,
  review_id        TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,
  relation         TEXT NOT NULL,
  provider         TEXT NOT NULL DEFAULT '',
  external_id      TEXT NOT NULL DEFAULT '',
  target_review_id TEXT NOT NULL DEFAULT '',
  url              TEXT NOT NULL DEFAULT '',
  title            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT '',
  source           TEXT NOT NULL DEFAULT 'explicit',
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE(review_id, kind, relation, provider, external_id, target_review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_relationships_review
  ON review_relationships(review_id, kind, relation);
