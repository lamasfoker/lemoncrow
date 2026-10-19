-- Stored results derived from one Snapshot that every read would otherwise
-- recompute from its units and packet (Snapshot history rows, inbox progress).
-- Each row keeps the stamp of the exact inputs and deriving code. A row whose
-- inputs changed is always recomputed. One made by older deriving code may be
-- served while it is refreshed, newest first, within a per-request compute
-- budget. The Snapshot's deletion cascade owns retention.
CREATE TABLE IF NOT EXISTS review_derived (
  revision_id   TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  scope_key     TEXT NOT NULL DEFAULT '',
  review_id     TEXT NOT NULL,
  stamp         TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (revision_id, kind, scope_key)
);
CREATE INDEX IF NOT EXISTS idx_review_derived_kind ON review_derived(kind, revision_id);
