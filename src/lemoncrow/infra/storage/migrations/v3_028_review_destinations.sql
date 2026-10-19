-- Where a Change's review feedback goes when the reviewer sends it (spec
-- docs/superpowers/specs/2026-09-29-review-handoff-design.md). One row per
-- Review, only while the reviewer has pinned something other than the default
-- exact-author routing; unpinning deletes the row. The Review's deletion
-- cascade owns retention.
CREATE TABLE IF NOT EXISTS review_destinations (
  review_id     TEXT PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  host          TEXT NOT NULL DEFAULT '',
  session_id    TEXT NOT NULL DEFAULT '',
  external_ref  TEXT NOT NULL DEFAULT '',
  set_by        TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL
);
