-- Run in Supabase SQL editor if not using Alembic (same as migration 006_openai_response_logs).

CREATE TABLE IF NOT EXISTS sarah.openai_response_logs (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES sarah.conversations(id) ON DELETE CASCADE,
    turn_id UUID NOT NULL,
    round_index INTEGER NOT NULL,
    openai_response_id VARCHAR(128),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_sarah_openai_response_logs_conversation_id
    ON sarah.openai_response_logs (conversation_id);
CREATE INDEX IF NOT EXISTS ix_sarah_openai_response_logs_turn_id
    ON sarah.openai_response_logs (turn_id);
