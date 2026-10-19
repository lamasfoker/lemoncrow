-- Cumulative AI spend per Review (review/synthesis.py, spec §10): one row per
-- Review counting model calls, so the budget survives synthesis-cache pruning.
-- The session deletion flow (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_synthesis_spend (
  review_id    TEXT NOT NULL PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  model_calls  INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL DEFAULT ''
);
