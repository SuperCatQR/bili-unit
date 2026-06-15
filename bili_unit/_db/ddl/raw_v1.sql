-- bili_unit raw DB schema, version 1.
--
-- One file per uid: data/bili/{uid}.raw.db.
-- Holds B站 64 endpoint raw responses + fetch progress cursors.
-- Producer-private — consumers do NOT need to attach this database.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Required keys: schema_version, uid.

-- Raw endpoint payload.
--   item_id = '' for endpoint-level responses (most endpoints).
--   item_id = bvid / cvid / opus_id / dynamic_id / rlid for fan-out endpoints.
-- A paginated endpoint stores its merged {pages: [...]} dict at item_id=''.
CREATE TABLE IF NOT EXISTS raw_payload (
    endpoint      TEXT NOT NULL,
    item_id       TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL,
    fetched_at_ms INTEGER NOT NULL,
    PRIMARY KEY (endpoint, item_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_endpoint
    ON raw_payload(endpoint, fetched_at_ms);

-- Pagination cursor / progress per endpoint. Acts as the commit marker for
-- write_pair_locked: payload is written first, progress last; if we crash
-- between the two writes, progress is stale and the next resume re-fetches
-- from the old cursor (idempotent overwrite).
CREATE TABLE IF NOT EXISTS fetch_progress (
    endpoint      TEXT PRIMARY KEY,
    cursor        TEXT,
    total         INTEGER,
    fetched       INTEGER,
    updated_at_ms INTEGER NOT NULL
);
