-- Author guidance (review/guidance.py): raw, attributed, versioned author
-- input imported with the Change. Immutable rows, and the session deletion flow
-- (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_author_guidance (
  review_id            TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                   TEXT NOT NULL,
  revision_id          TEXT NOT NULL DEFAULT '',
  producer_host        TEXT NOT NULL DEFAULT '',
  producer_session_id  TEXT NOT NULL DEFAULT '',
  producer_model       TEXT NOT NULL DEFAULT '',
  attribution          TEXT NOT NULL DEFAULT 'unattributed',
  repo_root            TEXT NOT NULL DEFAULT '',
  schema_version       INTEGER NOT NULL DEFAULT 1,
  source_identity_json TEXT NOT NULL DEFAULT '[]',
  payload_json         TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  imported_at          TEXT NOT NULL,
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_author_guidance_producer
  ON review_author_guidance(review_id, producer_host, producer_session_id, created_at);
