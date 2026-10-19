-- Durable hard-delete tombstones (P12-AUTO item 12). Written by the store in
-- the same transaction as a permanent Review deletion and deliberately NOT a
-- child of review_sessions (no FK), so a tombstone outlives the rows it names.
-- Append-only and chained per store: record_sha256 covers every other column,
-- previous_sha256 is the prior tombstone's record_sha256. Retention: kept for
-- the life of the store (hosted: removed only by organization deletion).
CREATE TABLE IF NOT EXISTS review_deletion_tombstones (
    sequence INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    review_id TEXT NOT NULL,
    repo_id TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    confirmation_sha256 TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    deleted_at TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL DEFAULT '',
    record_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_deletion_tombstones_review ON review_deletion_tombstones(review_id);
