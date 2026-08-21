-- 会议助手首版关系模型。
-- __SCHEMA__ is replaced only after strict PostgreSQL identifier validation.

CREATE TABLE IF NOT EXISTS __SCHEMA__.schema_migrations (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS __SCHEMA__.meetings (
    id uuid PRIMARY KEY,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    status text NOT NULL CHECK (status IN (
        'recording', 'finalizing', 'completed', 'interrupted', 'storage_error'
    )),
    language text NOT NULL CHECK (char_length(language) BETWEEN 1 AND 32),
    audio_source text NOT NULL CHECK (audio_source = 'microphone'),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    transcript_revision bigint NOT NULL DEFAULT 0 CHECK (transcript_revision >= 0),
    content_revision bigint NOT NULL DEFAULT 0 CHECK (content_revision >= 0),
    interruption_reason text CHECK (interruption_reason IS NULL OR char_length(interruption_reason) <= 128),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS __SCHEMA__.meeting_speakers (
    meeting_id uuid NOT NULL REFERENCES __SCHEMA__.meetings(id) ON DELETE CASCADE,
    speaker_key text NOT NULL CHECK (char_length(speaker_key) BETWEEN 1 AND 200),
    source_epoch integer NOT NULL CHECK (source_epoch >= 0),
    raw_speaker text NOT NULL CHECK (char_length(raw_speaker) BETWEEN 1 AND 200),
    default_label text NOT NULL CHECK (char_length(default_label) BETWEEN 1 AND 200),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, speaker_key)
);

CREATE TABLE IF NOT EXISTS __SCHEMA__.transcript_segments (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES __SCHEMA__.meetings(id) ON DELETE CASCADE,
    segment_order integer NOT NULL CHECK (segment_order >= 0),
    source_epoch integer NOT NULL CHECK (source_epoch >= 0),
    speaker_key text NOT NULL CHECK (char_length(speaker_key) BETWEEN 1 AND 200),
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL CHECK (end_ms >= start_ms),
    text text NOT NULL CHECK (char_length(btrim(text)) > 0),
    translation text,
    detected_language text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS __SCHEMA__.meeting_minutes (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES __SCHEMA__.meetings(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version >= 1),
    status text NOT NULL CHECK (status IN ('queued', 'generating', 'completed', 'failed')),
    source_content_revision bigint NOT NULL CHECK (source_content_revision >= 0),
    model text NOT NULL DEFAULT '',
    prompt_version text NOT NULL DEFAULT 'v1',
    idempotency_key text,
    content_json jsonb,
    content_markdown text,
    raw_output text,
    error_code text,
    error_message text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    generated_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (meeting_id, version),
    UNIQUE (meeting_id, id),
    UNIQUE (meeting_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS __SCHEMA__.meeting_events (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES __SCHEMA__.meetings(id) ON DELETE CASCADE,
    event_type text NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 128),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS meetings_created_at_idx
    ON __SCHEMA__.meetings (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS transcript_segments_meeting_order_idx
    ON __SCHEMA__.transcript_segments (meeting_id, segment_order);
CREATE INDEX IF NOT EXISTS transcript_segments_meeting_start_idx
    ON __SCHEMA__.transcript_segments (meeting_id, start_ms);
CREATE INDEX IF NOT EXISTS transcript_segments_speaker_start_idx
    ON __SCHEMA__.transcript_segments (meeting_id, speaker_key, start_ms);
CREATE INDEX IF NOT EXISTS meeting_minutes_queue_idx
    ON __SCHEMA__.meeting_minutes (status, lease_until, created_at);
CREATE INDEX IF NOT EXISTS meeting_events_meeting_time_idx
    ON __SCHEMA__.meeting_events (meeting_id, occurred_at);
