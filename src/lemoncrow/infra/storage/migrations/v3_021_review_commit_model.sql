-- Review identity is separate from mutable branch/worktree state.
-- Existing captures remain explicitly legacy. Phase 2 classifies new captures
-- as drafts or commit checkpoints without guessing about historical rows.
ALTER TABLE review_sessions ADD COLUMN review_base_sha TEXT NOT NULL DEFAULT '';
DROP INDEX IF EXISTS ux_review_sessions_subject;
CREATE INDEX IF NOT EXISTS idx_review_sessions_legacy_subject
  ON review_sessions(repo_root, subject_type, source_ref, range_mode);

ALTER TABLE review_revisions ADD COLUMN capture_kind TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE review_revisions ADD COLUMN commit_sha TEXT NOT NULL DEFAULT '';
ALTER TABLE review_revisions ADD COLUMN branch_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE review_revisions ADD COLUMN worktree_key TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS review_bindings (
  id                  TEXT PRIMARY KEY,
  repository_identity TEXT NOT NULL,
  subject_kind        TEXT NOT NULL,
  subject_key         TEXT NOT NULL,
  review_id           TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  worktree_key        TEXT NOT NULL DEFAULT '',
  active              INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  UNIQUE(repository_identity, subject_kind, subject_key, review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_bindings_subject
  ON review_bindings(repository_identity, subject_kind, subject_key, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_bindings_review
  ON review_bindings(review_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_commits (
  review_id      TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  commit_number  INTEGER NOT NULL,
  commit_sha     TEXT NOT NULL,
  revision_id    TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  parent_sha     TEXT NOT NULL DEFAULT '',
  state          TEXT NOT NULL DEFAULT 'active',
  created_at     TEXT NOT NULL,
  PRIMARY KEY (review_id, commit_number),
  UNIQUE(review_id, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_review_commits_revision ON review_commits(revision_id);
CREATE INDEX IF NOT EXISTS idx_review_commits_state ON review_commits(review_id, state, commit_number);
