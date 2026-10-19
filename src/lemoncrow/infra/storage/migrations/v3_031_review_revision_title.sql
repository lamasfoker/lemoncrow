-- Snapshot display metadata derived from captured author-visible output.
ALTER TABLE review_revisions ADD COLUMN title TEXT NOT NULL DEFAULT '';
