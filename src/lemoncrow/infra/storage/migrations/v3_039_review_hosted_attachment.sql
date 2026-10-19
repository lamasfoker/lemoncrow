-- Local <-> hosted attachment of one Review (G5-SYNC, production guarantees
-- plan "G5-SYNC contract"). Local stores only: a hosted store never holds an
-- attachment (HostedReviewStore.hosted_attachment_supported() is False).
--
-- review_hosted_attachments: one row per explicitly attached local Review.
--   Writer: ReviewStore.attach_hosted (re-attaching the same identity refreshes
--   the projection, bounded by hosted_attachment.PROJECTION_*_LIMIT).
--   Closer: ReviewStore.detach_hosted, and the Review deletion cascade.
-- review_hosted_actions: explicitly queued local actions bound to the hosted
--   Snapshot they were made on, plus their replay result.
--   Writer: ReviewStore.add_hosted_action / transition_hosted_action.
--   Closer: at most hosted_attachment.UNRESOLVED_ACTION_LIMIT unresolved
--   (pending|conflict) rows per Review; resolved rows pruned by
--   ReviewStore.prune_hosted_actions (count and age); detach; Review deletion.
CREATE TABLE IF NOT EXISTS review_hosted_attachments (
  review_id        TEXT PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  origin           TEXT NOT NULL,
  hosted_repo_id   TEXT NOT NULL,
  hosted_review_id TEXT NOT NULL,
  principal_id     TEXT NOT NULL,
  projection_json  TEXT NOT NULL DEFAULT '{}',
  attached_at      TEXT NOT NULL,
  synced_at        TEXT NOT NULL DEFAULT ''
);
-- One local Review per hosted Change in a store: one identity, never two local
-- Reviews both claiming the same hosted Review.
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_hosted_attachments_hosted
  ON review_hosted_attachments(origin, hosted_repo_id, hosted_review_id);
CREATE INDEX IF NOT EXISTS idx_review_hosted_attachments_hosted_review
  ON review_hosted_attachments(hosted_review_id);

CREATE TABLE IF NOT EXISTS review_hosted_actions (
  id                 TEXT PRIMARY KEY,
  review_id          TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  sequence           INTEGER NOT NULL,
  kind               TEXT NOT NULL
                     CHECK (kind IN ('mark', 'comment', 'reply', 'outcome', 'request_complete')),
  snapshot_id        TEXT NOT NULL,
  supersede_key      TEXT NOT NULL DEFAULT '',
  payload_json       TEXT NOT NULL,
  preconditions_json TEXT NOT NULL,
  state              TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'applied', 'conflict', 'discarded')),
  reason             TEXT NOT NULL DEFAULT '',
  result_json        TEXT NOT NULL DEFAULT '{}',
  created_at         TEXT NOT NULL,
  resolved_at        TEXT NOT NULL DEFAULT '',
  UNIQUE (review_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_review_hosted_actions_state
  ON review_hosted_actions(review_id, state, sequence);
