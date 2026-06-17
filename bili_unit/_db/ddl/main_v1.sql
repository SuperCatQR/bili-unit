-- bili_unit main DB schema, version 1.
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
    level         INTEGER,
    follower      INTEGER,
    following     INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS video (
    bvid          TEXT PRIMARY KEY,
    aid           INTEGER,
    title         TEXT,
    description   TEXT,
    cover_url     TEXT,
    duration_s    INTEGER,
    pubdate_ms    INTEGER,
    view_count    INTEGER,
    danmaku       INTEGER,
    reply         INTEGER,
    favorite      INTEGER,
    coin          INTEGER,
    share         INTEGER,
    like_count    INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_pubdate ON video(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS video_page (
    bvid       TEXT NOT NULL,
    page_no    INTEGER NOT NULL,
    cid        INTEGER,
    part       TEXT,
    duration_s INTEGER,
    PRIMARY KEY (bvid, page_no),
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS video_subtitle (
    bvid          TEXT PRIMARY KEY,
    has_official  INTEGER NOT NULL CHECK (has_official IN (0, 1)),
    has_ai        INTEGER NOT NULL CHECK (has_ai IN (0, 1)),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article (
    cvid          TEXT PRIMARY KEY,
    title         TEXT,
    summary       TEXT,
    pubdate_ms    INTEGER,
    view_count    INTEGER,
    like_count    INTEGER,
    reply         INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_pubdate ON article(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS opus_post (
    opus_id       TEXT PRIMARY KEY,
    pubdate_ms    INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opus_pubdate ON opus_post(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS dynamic_event (
    dynamic_id    TEXT PRIMARY KEY,
    type          TEXT,
    pubdate_ms    INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dynamic_pubdate ON dynamic_event(pubdate_ms DESC);

CREATE TABLE IF NOT EXISTS audio_transcription (
    bvid                  TEXT PRIMARY KEY,
    status                TEXT NOT NULL
                          CHECK (status IN ('pending','running','success','failed','skipped')),
    transcription_source  TEXT,
    transcript            TEXT,
    audio_tokens          INTEGER,
    seconds               REAL,
    cache_hits            INTEGER,
    payload               TEXT NOT NULL,
    processed_at_ms       INTEGER NOT NULL,
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS image_asset (
    url_hash         TEXT PRIMARY KEY,
    source_kind      TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    url              TEXT NOT NULL,
    file_path        TEXT,
    bytes            INTEGER,
    data             BLOB,
    status           TEXT NOT NULL,
    downloaded_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_source ON image_asset(source_kind, source_id);

-- ---------------------------------------------------------------------------
-- Producer state (debuggability — not part of the consumer contract)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stage_task (
    stage         TEXT PRIMARY KEY
                  CHECK (stage IN ('fetching','parsing','processing')),
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_endpoint_state (
    endpoint        TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error_id   INTEGER,
    item_progress   TEXT,
    progress        TEXT,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_error (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stage          TEXT NOT NULL CHECK (stage IN ('fetching','processing')),
    endpoint       TEXT,
    pipeline       TEXT,
    item_type      TEXT,
    item_id        TEXT,
    error_type     TEXT NOT NULL,
    message        TEXT NOT NULL,
    retryable      INTEGER,
    detail         TEXT,
    occurred_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_error_stage
    ON stage_error(stage, occurred_at_ms);

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
    (SELECT COUNT(*) FROM stage_error WHERE stage = 'processing')
                                                                AS processing_error_count;
