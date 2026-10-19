-- Shared Review Intelligence run/cache identity and universal model-call spend.
CREATE TABLE IF NOT EXISTS review_intelligence_runs (
  review_id         TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                TEXT NOT NULL,
  revision_id       TEXT NOT NULL,
  snapshot_identity TEXT NOT NULL,
  capability        TEXT NOT NULL,
  run_key           TEXT NOT NULL,
  scope_digest      TEXT NOT NULL,
  context_digest    TEXT NOT NULL,
  policy_digest     TEXT NOT NULL,
  schema_digest     TEXT NOT NULL,
  driver            TEXT NOT NULL,
  model             TEXT NOT NULL DEFAULT '',
  prompt_version    INTEGER NOT NULL DEFAULT 1,
  schema_version    INTEGER NOT NULL DEFAULT 1,
  status            TEXT NOT NULL,
  result_json       TEXT NOT NULL DEFAULT '{}',
  usage_json        TEXT NOT NULL DEFAULT '{}',
  error             TEXT NOT NULL DEFAULT '',
  model_calls       INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  started_at        TEXT NOT NULL DEFAULT '',
  completed_at      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_intelligence_runs_key
  ON review_intelligence_runs(review_id, run_key, created_at);
CREATE INDEX IF NOT EXISTS idx_review_intelligence_runs_revision
  ON review_intelligence_runs(review_id, revision_id, capability, created_at);

CREATE TABLE IF NOT EXISTS review_intelligence_spend (
  review_id    TEXT NOT NULL PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  model_calls  INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL DEFAULT ''
);

INSERT INTO review_intelligence_spend (review_id, model_calls, updated_at)
SELECT review_id, model_calls, updated_at FROM review_synthesis_spend
WHERE 1
ON CONFLICT(review_id) DO UPDATE SET
  model_calls = CASE
    WHEN excluded.model_calls > review_intelligence_spend.model_calls
    THEN excluded.model_calls
    ELSE review_intelligence_spend.model_calls
  END,
  updated_at = CASE
    WHEN excluded.model_calls > review_intelligence_spend.model_calls
    THEN excluded.updated_at
    ELSE review_intelligence_spend.updated_at
  END;
