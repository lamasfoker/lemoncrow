-- Keep existing Reviews' integration behavior; --new freezes the original base.
ALTER TABLE review_sessions
  ADD COLUMN review_base_policy TEXT NOT NULL DEFAULT 'integrated';
