-- bili_unit main DB schema, version 3.
--
-- One file per uid: data/bili/{uid}.db.
-- Consumer contract: anything in this file is part of the public SQL surface.
-- Anything in {uid}.raw.db is producer-private (re-parse fuel).
--
-- Style:
--   * timestamps end with _ms and are INTEGER ms-epoch.
--   * payload TEXT columns hold the full JSON dict for the row (escape hatch
--     for fields not promoted to typed columns).
--   * stage_task / fetch_endpoint_state / stage_error are producer state, kept
--     here for debuggability; consumers normally read content tables only.

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
--                last_fetched_at_ms, last_parsed_at_ms, last_processed_at_ms.

-- ---------------------------------------------------------------------------
-- Content tables (consumer-facing)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_profile (
    uid           INTEGER PRIMARY KEY,
    name          TEXT,
    sign          TEXT,
    face_url      TEXT,
    level         INTEGER CHECK (level IS NULL OR level >= 0),
    follower      INTEGER CHECK (follower IS NULL OR follower >= 0),
    following     INTEGER CHECK (following IS NULL OR following >= 0),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL CHECK (parsed_at_ms >= 0)
);

CREATE TABLE IF NOT EXISTS video (
    bvid          TEXT PRIMARY KEY,
    aid           INTEGER,
    title         TEXT,
    description   TEXT,
    cover_url     TEXT,
    duration_s    INTEGER CHECK (duration_s IS NULL OR duration_s >= 0),
    pubdate_ms    INTEGER CHECK (pubdate_ms IS NULL OR pubdate_ms >= 0),
    view_count    INTEGER CHECK (view_count IS NULL OR view_count >= 0),
    danmaku       INTEGER CHECK (danmaku IS NULL OR danmaku >= 0),
    reply         INTEGER CHECK (reply IS NULL OR reply >= 0),
    favorite      INTEGER CHECK (favorite IS NULL OR favorite >= 0),
    coin          INTEGER CHECK (coin IS NULL OR coin >= 0),
    share         INTEGER CHECK (share IS NULL OR share >= 0),
    like_count    INTEGER CHECK (like_count IS NULL OR like_count >= 0),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL CHECK (parsed_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_video_pubdate ON video(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS video_page (
    bvid       TEXT NOT NULL,
    page_no    INTEGER NOT NULL CHECK (page_no > 0),
    cid        INTEGER CHECK (cid IS NULL OR cid >= 0),
    part       TEXT,
    duration_s INTEGER CHECK (duration_s IS NULL OR duration_s >= 0),
    PRIMARY KEY (bvid, page_no),
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS video_subtitle (
    bvid                                                  TEXT PRIMARY KEY,
    has_bilibili_human_uploaded_or_official_subtitle       INTEGER NOT NULL CHECK (has_bilibili_human_uploaded_or_official_subtitle IN (0, 1)),
    has_bilibili_platform_ai_generated_subtitle            INTEGER NOT NULL CHECK (has_bilibili_platform_ai_generated_subtitle IN (0, 1)),
    payload                                               TEXT NOT NULL,
    parsed_at_ms                                          INTEGER NOT NULL CHECK (parsed_at_ms >= 0),
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_subtitle_platform_ai
    ON video_subtitle(has_bilibili_platform_ai_generated_subtitle, bvid);

CREATE TABLE IF NOT EXISTS video_subtitle_page (
    bvid                                      TEXT NOT NULL,
    page_no                                   INTEGER NOT NULL CHECK (page_no > 0),
    bilibili_video_page_index                 INTEGER NOT NULL CHECK (bilibili_video_page_index >= 0),
    bilibili_video_page_cid                   INTEGER CHECK (bilibili_video_page_cid IS NULL OR bilibili_video_page_cid >= 0),
    selected_bilibili_subtitle_language_code  TEXT NOT NULL
                                              CHECK (length(trim(selected_bilibili_subtitle_language_code)) > 0),
    selected_bilibili_subtitle_language_name  TEXT,
    is_selected_bilibili_subtitle_platform_ai_generated
                                              INTEGER NOT NULL CHECK (is_selected_bilibili_subtitle_platform_ai_generated IN (0, 1)),
    selected_bilibili_subtitle_text           TEXT,
    subtitle_segment_count                    INTEGER NOT NULL CHECK (subtitle_segment_count >= 0),
    parsed_at_ms                              INTEGER NOT NULL CHECK (parsed_at_ms >= 0),
    PRIMARY KEY (bvid, page_no),
    FOREIGN KEY (bvid) REFERENCES video_subtitle(bvid) ON DELETE CASCADE,
    FOREIGN KEY (bvid, page_no) REFERENCES video_page(bvid, page_no) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_subtitle_page_language
    ON video_subtitle_page(selected_bilibili_subtitle_language_code, bvid);
CREATE INDEX IF NOT EXISTS idx_video_subtitle_page_platform_ai
    ON video_subtitle_page(is_selected_bilibili_subtitle_platform_ai_generated, bvid);

CREATE TABLE IF NOT EXISTS video_subtitle_segment (
    bvid                            TEXT NOT NULL,
    page_no                         INTEGER NOT NULL CHECK (page_no > 0),
    segment_no                      INTEGER NOT NULL CHECK (segment_no > 0),
    bilibili_subtitle_start_seconds REAL NOT NULL CHECK (bilibili_subtitle_start_seconds >= 0),
    bilibili_subtitle_end_seconds   REAL NOT NULL CHECK (bilibili_subtitle_end_seconds >= bilibili_subtitle_start_seconds),
    bilibili_subtitle_duration_seconds
                                     REAL NOT NULL CHECK (bilibili_subtitle_duration_seconds >= 0),
    bilibili_subtitle_segment_text  TEXT NOT NULL,
    PRIMARY KEY (bvid, page_no, segment_no),
    FOREIGN KEY (bvid, page_no)
        REFERENCES video_subtitle_page(bvid, page_no) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article (
    cvid          TEXT PRIMARY KEY,
    title         TEXT,
    summary       TEXT,
    pubdate_ms    INTEGER CHECK (pubdate_ms IS NULL OR pubdate_ms >= 0),
    view_count    INTEGER CHECK (view_count IS NULL OR view_count >= 0),
    like_count    INTEGER CHECK (like_count IS NULL OR like_count >= 0),
    reply         INTEGER CHECK (reply IS NULL OR reply >= 0),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL CHECK (parsed_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_article_pubdate ON article(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS opus_post (
    opus_id       TEXT PRIMARY KEY,
    pubdate_ms    INTEGER CHECK (pubdate_ms IS NULL OR pubdate_ms >= 0),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL CHECK (parsed_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_opus_pubdate ON opus_post(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS dynamic_event (
    dynamic_id    TEXT PRIMARY KEY,
    type          TEXT,
    pubdate_ms    INTEGER CHECK (pubdate_ms IS NULL OR pubdate_ms >= 0),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL CHECK (parsed_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_dynamic_pubdate ON dynamic_event(pubdate_ms DESC);

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
    processed_at_ms       INTEGER NOT NULL CHECK (processed_at_ms >= 0),
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
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
    FOREIGN KEY (bvid) REFERENCES audio_transcription(bvid) ON DELETE CASCADE,
    FOREIGN KEY (bvid, page_no) REFERENCES video_page(bvid, page_no) ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS image_asset (
    url_hash         TEXT PRIMARY KEY,
    source_kind      TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    url              TEXT NOT NULL,
    file_path        TEXT,
    bytes            INTEGER CHECK (bytes IS NULL OR bytes >= 0),
    data             BLOB,
    status           TEXT NOT NULL CHECK (status IN ('ok','failed','skipped')),
    downloaded_at_ms INTEGER NOT NULL CHECK (downloaded_at_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_image_source ON image_asset(source_kind, source_id);

-- ---------------------------------------------------------------------------
-- Producer state (debuggability — not part of the consumer contract)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stage_task (
    stage         TEXT PRIMARY KEY
                  CHECK (stage IN ('fetching','parsing','asr')),
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
    stage          TEXT NOT NULL CHECK (stage IN ('fetching','parsing','asr')),
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
                      'PENDING','RUNNING','SUCCESS','PARTIAL','FAILED','CANCELLED'
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
-- Views (replace the old _manifest / _aggregates compute paths)
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS video_full AS
SELECT v.bvid,
       v.aid, v.title, v.description, v.cover_url, v.duration_s, v.pubdate_ms,
       v.view_count, v.danmaku, v.reply, v.favorite, v.coin, v.share, v.like_count,
       v.payload AS video_payload,
       v.parsed_at_ms,
       t.status               AS transcription_status,
       t.transcription_source,
       t.transcript,
       t.audio_tokens, t.seconds, t.cache_hits,
       t.processed_at_ms
FROM video v
LEFT JOIN audio_transcription t USING (bvid);

CREATE VIEW IF NOT EXISTS manifest_summary AS
SELECT
    (SELECT value FROM meta WHERE key = 'uid')                  AS uid,
    (SELECT value FROM meta WHERE key = 'schema_version')       AS schema_version,
    (SELECT value FROM meta WHERE key = 'last_fetched_at_ms')   AS last_fetched_at_ms,
    (SELECT value FROM meta WHERE key = 'last_parsed_at_ms')    AS last_parsed_at_ms,
    (SELECT value FROM meta WHERE key = 'last_processed_at_ms') AS last_processed_at_ms,
    (SELECT COUNT(*) FROM video)                                AS video_count,
    (SELECT COUNT(*) FROM article)                              AS article_count,
    (SELECT COUNT(*) FROM opus_post)                            AS opus_count,
    (SELECT COUNT(*) FROM dynamic_event)                        AS dynamic_count,
    (SELECT COUNT(*) FROM audio_transcription WHERE status = 'success')
                                                                AS transcribed_count,
    (SELECT COUNT(*) FROM audio_transcription WHERE status = 'failed')
                                                                AS transcription_failed_count,
    (SELECT COALESCE(SUM(audio_tokens), 0) FROM audio_transcription)
                                                                AS total_audio_tokens,
    (SELECT COALESCE(SUM(seconds),      0) FROM audio_transcription)
                                                                AS total_audio_seconds,
    (SELECT COALESCE(SUM(cache_hits),   0) FROM audio_transcription)
                                                                AS total_cache_hits,
    (SELECT COUNT(*) FROM stage_error WHERE stage = 'fetching')
                                                                AS fetching_error_count,
    (SELECT COUNT(*) FROM stage_error WHERE stage = 'asr')
                                                                AS asr_error_count;
