-- Durable Review Ask conversations. Legacy messages keep conversation_id='' and
-- are projected as one synthetic Previous conversation by the API.
CREATE TABLE IF NOT EXISTS review_ask_conversations (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    origin_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_ask_conversations_review
    ON review_ask_conversations(review_id, updated_at DESC, id DESC);
ALTER TABLE review_ask_messages ADD COLUMN conversation_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_review_ask_messages_conversation
    ON review_ask_messages(review_id, conversation_id, created_at ASC, id ASC);
