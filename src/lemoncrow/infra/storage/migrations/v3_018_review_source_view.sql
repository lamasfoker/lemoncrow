ALTER TABLE review_revisions ADD COLUMN source_view_id TEXT NOT NULL DEFAULT '';
ALTER TABLE review_revisions ADD COLUMN source_view_revision INTEGER NOT NULL DEFAULT 0;
