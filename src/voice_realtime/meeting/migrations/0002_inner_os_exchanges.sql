-- 用户显式保存的 Inner OS 规范化问答；不保存临时背景、原始模型输出或推理过程。
CREATE TABLE IF NOT EXISTS __SCHEMA__.meeting_inner_os_exchanges (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES __SCHEMA__.meetings(id) ON DELETE CASCADE,
    question text NOT NULL CHECK (char_length(btrim(question)) BETWEEN 1 AND 2000),
    intent text NOT NULL CHECK (intent IN ('fact', 'analysis', 'draft', 'mixed')),
    answer_json jsonb NOT NULL,
    source_transcript_revision bigint NOT NULL CHECK (source_transcript_revision >= 0),
    source_content_revision bigint NOT NULL CHECK (source_content_revision >= 0),
    used_ephemeral_context boolean NOT NULL DEFAULT false,
    model text NOT NULL,
    reasoning text NOT NULL CHECK (reasoning IN ('off', 'on')),
    prompt_version text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS meeting_inner_os_exchanges_page_idx
    ON __SCHEMA__.meeting_inner_os_exchanges (meeting_id, created_at DESC, id DESC);
