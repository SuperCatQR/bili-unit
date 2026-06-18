from __future__ import annotations

import sqlite3
from pathlib import Path

from bili_unit._db.connection import SUPPORTED_MAIN_SCHEMA_VERSION
from tools.migrate_legacy_asr_text import migrate_uid


def _create_legacy_db(root: Path, uid: int) -> None:
    conn = sqlite3.connect(root / f"{uid}.db")
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        conn.execute(
            "CREATE TABLE audio_transcription("
            "bvid TEXT PRIMARY KEY, status TEXT, transcription_source TEXT, "
            "transcript TEXT, audio_tokens INTEGER, seconds REAL, cache_hits INTEGER, "
            "payload TEXT, processed_at_ms INTEGER)"
        )
        conn.execute(
            "INSERT INTO audio_transcription VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVtext",
                "success",
                "MIMO-ASR",
                "valuable transcript",
                12,
                3.5,
                1,
                '{"legacy":true}',
                1000,
            ),
        )
        conn.execute(
            "INSERT INTO audio_transcription VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("BVempty", "failed", None, None, None, None, None, "{}", 1001),
        )
        conn.commit()
    finally:
        conn.close()
    raw = sqlite3.connect(root / f"{uid}.raw.db")
    try:
        raw.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        raw.commit()
    finally:
        raw.close()


def test_migrate_uid_preserves_only_legacy_asr_text(tmp_path: Path) -> None:
    uid = 123
    _create_legacy_db(tmp_path, uid)

    result = migrate_uid(uid, root=tmp_path, backup_label="test")

    assert result.migrated is True
    assert result.source_schema_version == 1
    assert result.asr_rows == 1
    assert result.backup_dir is not None
    assert (result.backup_dir / f"{uid}.db").exists()
    assert (result.backup_dir / f"{uid}.raw.db").exists()

    conn = sqlite3.connect(tmp_path / f"{uid}.db")
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()[0]
        assert int(version) == SUPPORTED_MAIN_SCHEMA_VERSION
        videos = conn.execute("SELECT bvid, title FROM video").fetchall()
        assert [dict(row) for row in videos] == [
            {"bvid": "BVtext", "title": "legacy ASR placeholder BVtext"},
        ]
        rows = conn.execute(
            "SELECT bvid, status, transcription_source, transcript, "
            "audio_tokens, seconds, cache_hits, processed_at_ms "
            "FROM audio_transcription",
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "bvid": "BVtext",
                "status": "success",
                "transcription_source": "MIMO-ASR",
                "transcript": "valuable transcript",
                "audio_tokens": 12,
                "seconds": 3.5,
                "cache_hits": 1,
                "processed_at_ms": 1000,
            },
        ]
    finally:
        conn.close()


def test_migrate_uid_skips_current_schema(tmp_path: Path) -> None:
    uid = 456
    conn = sqlite3.connect(tmp_path / f"{uid}.db")
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SUPPORTED_MAIN_SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()

    result = migrate_uid(uid, root=tmp_path, backup_label="test")

    assert result.migrated is False
    assert result.backup_dir is None
