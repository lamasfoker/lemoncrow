-- Plan synthesis cache (review/synthesis.py): one row per exact input key.
-- Pruned to a bounded history, and the session deletion flow (ON DELETE
-- CASCADE) owns retention. Deleting a row never deletes a human judgment.
CREATE TABLE IF NOT EXISTS review_plan_syntheses (
  review_id       TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id              TEXT NOT NULL,
  revision_id     TEXT NOT NULL,
  synthesis_key   TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  prompt_version  INTEGER NOT NULL DEFAULT 1,
  model           TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL,
  payload_json    TEXT NOT NULL DEFAULT '{}',
  error           TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  completed_at    TEXT NOT NULL DEFAULT '',
  inputs_digest   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_plan_syntheses_key
  ON review_plan_syntheses(review_id, synthesis_key, created_at);
CREATE INDEX IF NOT EXISTS idx_review_plan_syntheses_inputs
  ON review_plan_syntheses(review_id, revision_id, inputs_digest, created_at);
