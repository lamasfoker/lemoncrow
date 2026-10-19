-- Change-level Development (see review/development.py). Append-only events
-- with a per-Change sequence. A Snapshot freezes its Development by recording
-- the event watermark in the same transaction that creates the revision.
CREATE TABLE IF NOT EXISTS development_sessions (
  change_id         TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  host              TEXT NOT NULL,
  session_id        TEXT NOT NULL,
  parent_session_id TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  purpose           TEXT NOT NULL DEFAULT '',
  role              TEXT NOT NULL DEFAULT 'observer',
  binding           TEXT NOT NULL DEFAULT 'observed',
  task              TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT '',
  started_at        TEXT NOT NULL DEFAULT '',
  ended_at          TEXT NOT NULL DEFAULT '',
  cursor_json       TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (change_id, host, session_id)
);
CREATE INDEX IF NOT EXISTS idx_development_sessions_session
  ON development_sessions(host, session_id);

CREATE TABLE IF NOT EXISTS development_events (
  change_id        TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  sequence         INTEGER NOT NULL,
  event_id         TEXT NOT NULL,
  host             TEXT NOT NULL DEFAULT '',
  session_id       TEXT NOT NULL DEFAULT '',
  kind             TEXT NOT NULL,
  at               TEXT NOT NULL DEFAULT '',
  path             TEXT NOT NULL DEFAULT '',
  symbol           TEXT NOT NULL DEFAULT '',
  target_unit_key  TEXT NOT NULL DEFAULT '',
  summary          TEXT NOT NULL DEFAULT '',
  detail           TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT '',
  role             TEXT NOT NULL DEFAULT '',
  source           TEXT NOT NULL DEFAULT '',
  source_event_id  TEXT NOT NULL,
  causation_id     TEXT NOT NULL DEFAULT '',
  parent_event_id  TEXT NOT NULL DEFAULT '',
  annotation_id    TEXT NOT NULL DEFAULT '',
  task_id          TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  PRIMARY KEY (change_id, sequence),
  UNIQUE (change_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_development_events_session
  ON development_events(change_id, host, session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_path
  ON development_events(change_id, path, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_kind
  ON development_events(change_id, kind, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_causation
  ON development_events(change_id, causation_id);

CREATE TABLE IF NOT EXISTS development_projections (
  change_id  TEXT PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  cursor     INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS development_snapshots (
  revision_id     TEXT PRIMARY KEY REFERENCES review_revisions(id) ON DELETE CASCADE,
  change_id       TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  watermark       INTEGER NOT NULL DEFAULT 0,
  projection_json TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_development_snapshots_change
  ON development_snapshots(change_id, watermark);
