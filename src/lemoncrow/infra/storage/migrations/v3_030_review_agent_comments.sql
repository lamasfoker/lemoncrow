-- Producer metadata never changes a comment's trust source or human judgment.
ALTER TABLE annotations ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE annotations ADD COLUMN agent_host TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN agent_model TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN parent_session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN source_message_id TEXT NOT NULL DEFAULT '';
-- SCHEMA owns the additive discussion/import tables; review_sessions cascades own retention.
