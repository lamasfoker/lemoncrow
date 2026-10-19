-- Incremental "since Snapshot" Development projection checkpoint.
-- The frozen projection remains immutable. These fields cache only events
-- strictly newer than the Snapshot watermark so large histories can advance
-- without replaying the full tail on every read.
ALTER TABLE development_snapshots ADD COLUMN since_cursor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE development_snapshots ADD COLUMN since_json TEXT NOT NULL DEFAULT '';
