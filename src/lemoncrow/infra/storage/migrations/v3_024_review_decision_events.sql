-- HumanDecision events (review/decisions.py): append-only human dispositions.
-- The session deletion flow (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_decision_events (
  review_id                  TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                         TEXT NOT NULL,
  decision_key               TEXT NOT NULL,
  reviewer_id                TEXT NOT NULL DEFAULT 'local',
  revision_id                TEXT NOT NULL,
  action                     TEXT NOT NULL,
  disposition                TEXT NOT NULL,
  option_key                 TEXT NOT NULL DEFAULT '',
  definition_ref             TEXT NOT NULL DEFAULT '',
  semantic_fingerprint       TEXT NOT NULL DEFAULT '',
  implementation_fingerprint TEXT NOT NULL DEFAULT '',
  scope_json                 TEXT NOT NULL DEFAULT '{}',
  implementation_json        TEXT NOT NULL DEFAULT '{}',
  reason                     TEXT NOT NULL DEFAULT '',
  trigger_kind               TEXT NOT NULL DEFAULT '',
  trigger_check_key          TEXT NOT NULL DEFAULT '',
  trigger_note               TEXT NOT NULL DEFAULT '',
  feedback_annotation_id     TEXT NOT NULL DEFAULT '',
  prior_event_id             TEXT NOT NULL DEFAULT '',
  actor_type                 TEXT NOT NULL DEFAULT 'human',
  created_at                 TEXT NOT NULL,
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_decision_events_key
  ON review_decision_events(review_id, reviewer_id, decision_key, created_at, id);
