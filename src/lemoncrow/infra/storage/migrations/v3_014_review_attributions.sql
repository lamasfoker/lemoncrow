CREATE TABLE IF NOT EXISTS review_attributions (
  revision_id TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  scope       TEXT NOT NULL,
  scope_key   TEXT NOT NULL DEFAULT '',
  host        TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'contributor',
  model       TEXT NOT NULL DEFAULT '',
  certainty   TEXT NOT NULL DEFAULT 'none',
  method      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (revision_id, scope, scope_key, host, session_id)
);
CREATE INDEX IF NOT EXISTS idx_review_attributions_revision
  ON review_attributions(revision_id, scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_review_attributions_session
  ON review_attributions(host, session_id, created_at DESC);
