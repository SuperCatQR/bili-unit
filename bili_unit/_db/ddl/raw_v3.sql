-- bili_unit DB schema, version 3.
--
-- One file per uid: {BILI_DB_DIR}/{uid}.raw.db
-- (default: output/bili/{uid}.raw.db). This is the only DB file the unit
-- writes; there is no separate "main" DB anymore.
--
-- Style:
--   * timestamps end with _ms and are INTEGER ms-epoch.
--   * raw_payload.payload holds the original Bilibili API response JSON
--     verbatim. Consumers extract fields with json_extract() in their own
--     queries.
--   * audio_transcription.payload holds the full ProcessingItem dict for
--     the row's bvid (escape hatch for fields not promoted to columns).
--   * stage_task / stage_run / stage_event / stage_error / fetch_endpoint_state
--     are producer state, kept here for debuggability; consumers that only
--     want data read raw_payload + audio_transcription.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Meta
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Required keys: schema_version, uid, created_at_ms,
--                last_fetched_at_ms, last_processed_at_ms.

-- ---------------------------------------------------------------------------
-- Raw API payloads (consumer contract)
-- ---------------------------------------------------------------------------

-- Raw endpoint payload.
--   item_id = '' for endpoint-level responses (most endpoints).
--   item_id = bvid / cvid / opus_id / dynamic_id / rlid for fan-out endpoints.
-- A paginated endpoint stores its merged {pages: [...]} dict at item_id=''.
CREATE TABLE IF NOT EXISTS raw_payload (
    endpoint      TEXT NOT NULL,
    item_id       TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL,
    fetched_at_ms INTEGER NOT NULL CHECK (fetched_at_ms >= 0),
    PRIMARY KEY (endpoint, item_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_endpoint
    ON raw_payload(endpoint, fetched_at_ms);

-- Pagination cursor / progress per endpoint. Acts as the commit marker for
-- the fetching write_pair: payload is written first, progress last; if we
-- crash between the two writes, progress is stale and the next resume
-- re-fetches from the old cursor (idempotent overwrite).
CREATE TABLE IF NOT EXISTS fetch_progress (
    endpoint      TEXT PRIMARY KEY,
    cursor        TEXT,
    total         INTEGER CHECK (total IS NULL OR total >= 0),
    fetched       INTEGER CHECK (
                      fetched IS NULL OR fetched >= 0
                  ),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    CHECK (
        total IS NULL OR fetched IS NULL OR fetched <= total
    )
);

-- ---------------------------------------------------------------------------
-- ASR output (consumer contract)
-- ---------------------------------------------------------------------------
--
-- One row per bvid that has been dispatched to ASR. The bvid identity is
-- whatever the fetching layer recorded under raw_payload(endpoint='video_detail',
-- item_id=bvid) — we deliberately do NOT FK to a typed video table because
-- there is no typed video table anymore.

CREATE TABLE IF NOT EXISTS audio_transcription (
    bvid                  TEXT PRIMARY KEY,
    status                TEXT NOT NULL
                          CHECK (status IN ('pending','running','success','failed','skipped')),
    transcription_source  TEXT,
    transcript            TEXT,
    audio_tokens          INTEGER CHECK (audio_tokens IS NULL OR audio_tokens >= 0),
    seconds               REAL CHECK (seconds IS NULL OR seconds >= 0),
    cache_hits            INTEGER CHECK (cache_hits IS NULL OR cache_hits >= 0),
    payload               TEXT NOT NULL,
    processed_at_ms       INTEGER NOT NULL CHECK (processed_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_audio_transcription_status
    ON audio_transcription(status, bvid);
CREATE INDEX IF NOT EXISTS idx_audio_transcription_processed_at
    ON audio_transcription(processed_at_ms DESC);

CREATE TABLE IF NOT EXISTS audio_transcription_page (
    bvid                  TEXT NOT NULL,
    page_no               INTEGER NOT NULL CHECK (page_no > 0),
    page_index            INTEGER NOT NULL CHECK (page_index >= 0),
    cid                   INTEGER CHECK (cid IS NULL OR cid >= 0),
    duration_s            REAL CHECK (duration_s IS NULL OR duration_s >= 0),
    language              TEXT,
    asr_model             TEXT,
    transcript_text       TEXT,
    transcript_char_count INTEGER NOT NULL CHECK (transcript_char_count >= 0),
    segment_count         INTEGER NOT NULL CHECK (segment_count >= 0),
    PRIMARY KEY (bvid, page_no),
    FOREIGN KEY (bvid) REFERENCES audio_transcription(bvid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audio_transcription_page_language
    ON audio_transcription_page(language, bvid);

CREATE TABLE IF NOT EXISTS audio_transcription_segment (
    bvid                  TEXT NOT NULL,
    page_no               INTEGER NOT NULL CHECK (page_no > 0),
    segment_no            INTEGER NOT NULL CHECK (segment_no > 0),
    start_seconds         REAL CHECK (start_seconds IS NULL OR start_seconds >= 0),
    end_seconds           REAL CHECK (end_seconds IS NULL OR start_seconds IS NULL OR end_seconds >= start_seconds),
    duration_s            REAL CHECK (duration_s IS NULL OR duration_s >= 0),
    transcript_text       TEXT NOT NULL,
    language              TEXT,
    asr_model             TEXT,
    is_empty_transcript_skip
                          INTEGER NOT NULL DEFAULT 0 CHECK (is_empty_transcript_skip IN (0, 1)),
    is_high_risk_audio_skip
                          INTEGER NOT NULL DEFAULT 0 CHECK (is_high_risk_audio_skip IN (0, 1)),
    error_message         TEXT,
    PRIMARY KEY (bvid, page_no, segment_no),
    FOREIGN KEY (bvid, page_no)
        REFERENCES audio_transcription_page(bvid, page_no) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Producer state (debuggability — not part of the consumer contract)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stage_task (
    stage         TEXT PRIMARY KEY
                  CHECK (stage IN ('fetching','asr')),
    status        TEXT NOT NULL
                  CHECK (status IN (
                      'PENDING','RUNNING','SUCCESS','PARTIAL','FAILED',
                      'FAILED_RETRYABLE','FAILED_EXHAUSTED','FAILED_PERMANENT'
                  )),
    payload       TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
);

CREATE TABLE IF NOT EXISTS fetch_endpoint_state (
    endpoint        TEXT PRIMARY KEY,
    status          TEXT NOT NULL
                    CHECK (status IN (
                        'PENDING','RUNNING','SUCCESS','PARTIAL_ITEM',
                        'FAILED_RETRYABLE','FAILED_EXHAUSTED','FAILED_PERMANENT'
                    )),
    retry_count     INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error_id   INTEGER,
    item_progress   TEXT,
    progress        TEXT,
    updated_at_ms   INTEGER NOT NULL CHECK (updated_at_ms >= 0)
);

CREATE TABLE IF NOT EXISTS stage_error (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stage          TEXT NOT NULL CHECK (stage IN ('fetching','asr')),
    endpoint       TEXT,
    pipeline       TEXT,
    item_type      TEXT,
    item_id        TEXT,
    error_type     TEXT NOT NULL,
    message        TEXT NOT NULL,
    retryable      INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
    detail         TEXT,
    occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_stage_error_stage
    ON stage_error(stage, occurred_at_ms);
CREATE INDEX IF NOT EXISTS idx_stage_error_stage_recent
    ON stage_error(stage, id DESC);
CREATE INDEX IF NOT EXISTS idx_stage_error_endpoint
    ON stage_error(stage, endpoint, id DESC);
CREATE INDEX IF NOT EXISTS idx_stage_error_item
    ON stage_error(stage, pipeline, item_type, item_id, id DESC);

CREATE TABLE IF NOT EXISTS stage_run (
    run_id        TEXT PRIMARY KEY,
    uid           INTEGER NOT NULL,
    command       TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN (
                      'PENDING','RUNNING','SUCCESS','PARTIAL','FAILED','CANCELLED',
                      'DRY_RUN'
                  )),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
    ended_at_ms   INTEGER CHECK (ended_at_ms IS NULL OR ended_at_ms >= started_at_ms),
    args_json     TEXT NOT NULL,
    summary_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_run_uid_started
    ON stage_run(uid, started_at_ms DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS stage_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    ts_ms     INTEGER NOT NULL CHECK (ts_ms >= 0),
    level     TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    stage     TEXT NOT NULL,
    event     TEXT NOT NULL,
    endpoint  TEXT,
    pipeline  TEXT,
    item_type TEXT,
    item_id   TEXT,
    message   TEXT,
    data_json TEXT,
    FOREIGN KEY (run_id) REFERENCES stage_run(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stage_event_run_id
    ON stage_event(run_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_stage_event_item
    ON stage_event(stage, endpoint, pipeline, item_type, item_id);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------
--
-- manifest_summary aggregates basic counts so consumers can quickly tell what
-- is in the DB without grouping raw_payload themselves. Per-endpoint counts
-- live in raw_payload directly via:
--
--     SELECT endpoint, COUNT(*) FROM raw_payload GROUP BY endpoint;
--
-- The view uses CTEs to consolidate multiple table scans into one per table,
-- reducing 14 independent scalar subqueries to 3 aggregate CTEs + 4 meta
-- lookups. On 100K+ audio_transcription rows this gives ~2x speedup; at
-- typical sizes (1K–10K) the original was already sub-2ms so the difference
-- is in the noise.  No schema_version bump: view-only change, output identical.

CREATE VIEW IF NOT EXISTS manifest_summary AS
WITH
at_agg AS (
    SELECT
        COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0)               AS transcribed_count,
        COALESCE(SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END), 0)               AS transcription_failed_count,
        COALESCE(SUM(audio_tokens), 0)                                                  AS total_audio_tokens,
        COALESCE(SUM(seconds),       0)                                                 AS total_audio_seconds,
        COALESCE(SUM(cache_hits),    0)                                                 AS total_cache_hits
    FROM audio_transcription
),
rp_agg AS (
    SELECT
        COUNT(DISTINCT endpoint)                                                        AS endpoint_count,
        COUNT(*)                                                                        AS raw_payload_count,
        COALESCE(SUM(CASE WHEN endpoint = 'video_detail' THEN 1 ELSE 0 END), 0)        AS video_count
    FROM raw_payload
),
se_agg AS (
    SELECT
        COALESCE(SUM(CASE WHEN stage = 'fetching' THEN 1 ELSE 0 END), 0)               AS fetching_error_count,
        COALESCE(SUM(CASE WHEN stage = 'asr'      THEN 1 ELSE 0 END), 0)               AS asr_error_count
    FROM stage_error
)
SELECT
    (SELECT value FROM meta WHERE key = 'uid')                  AS uid,
    (SELECT value FROM meta WHERE key = 'schema_version')       AS schema_version,
    (SELECT value FROM meta WHERE key = 'last_fetched_at_ms')   AS last_fetched_at_ms,
    (SELECT value FROM meta WHERE key = 'last_processed_at_ms') AS last_processed_at_ms,
    rp_agg.endpoint_count,
    rp_agg.raw_payload_count,
    rp_agg.video_count,
    at_agg.transcribed_count,
    at_agg.transcription_failed_count,
    at_agg.total_audio_tokens,
    at_agg.total_audio_seconds,
    at_agg.total_cache_hits,
    se_agg.fetching_error_count,
    se_agg.asr_error_count
FROM at_agg, rp_agg, se_agg;
