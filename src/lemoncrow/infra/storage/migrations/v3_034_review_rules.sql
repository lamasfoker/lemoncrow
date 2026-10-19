-- Phase 6: typed rule provenance on findings plus inert suggestion dismissal state.
ALTER TABLE annotations ADD COLUMN rule_refs_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS review_rule_suggestion_states (
  id               TEXT PRIMARY KEY,
  repository_scope TEXT NOT NULL DEFAULT '',
  state            TEXT NOT NULL DEFAULT 'dismissed',
  actor_id         TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_rule_suggestion_states_scope
  ON review_rule_suggestion_states(repository_scope, state, updated_at DESC);
