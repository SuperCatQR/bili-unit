"""Consumer-facing SQLite contract tests.

These tests intentionally read through stdlib ``sqlite3`` instead of stage
stores. That mirrors the public read-side contract: the SQLite file is the
interface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import bili_unit
from bili_unit._db import UidContext

SAMPLE_UID = 3494380472109167


async def _seed_sample_uid(root: Path) -> Path:
    ctx = UidContext(SAMPLE_UID, root)
    await ctx.open()
    try:
        await ctx.main.execute(
            "INSERT OR REPLACE INTO user_profile("
            "uid, name, sign, face_url, level, follower, following, payload, parsed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SAMPLE_UID,
                "sample-up",
                "contract fixture",
                "https://example.com/face.jpg",
                6,
                42,
                7,
                json.dumps({"mid": SAMPLE_UID, "name": "sample-up"}),
                1000,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO video("
            "bvid, aid, title, description, cover_url, duration_s, pubdate_ms,"
            "view_count, danmaku, reply, favorite, coin, share, like_count,"
            "payload, parsed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                "contract video",
                "desc",
                "https://example.com/cover.jpg",
                120,
                1700000000000,
                10,
                1,
                2,
                3,
                4,
                5,
                6,
                json.dumps({"bvid": "BVCONTRACT1", "title": "contract video"}),
                1001,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO video_page("
            "bvid, page_no, cid, part, duration_s"
            ") VALUES (?, ?, ?, ?, ?)",
            ("BVCONTRACT1", 1, 12345, "P1", 120),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO video_subtitle("
            "bvid, has_official, has_ai, payload, parsed_at_ms"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                0,
                json.dumps({"bvid": "BVCONTRACT1", "pages": []}),
                1002,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO audio_transcription("
            "bvid, status, transcription_source, transcript, audio_tokens,"
            "seconds, cache_hits, payload, processed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                "success",
                "mock",
                "hello transcript",
                11,
                120.0,
                2,
                json.dumps({"item_id": "BVCONTRACT1", "status": "SUCCESS"}),
                1003,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_parsed_at_ms", "1002"),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_processed_at_ms", "1003"),
        )
    finally:
        await ctx.close()
    return bili_unit.db_path(SAMPLE_UID, settings=_settings(root))


def _settings(root: Path):
    from bili_unit import BiliSettings

    return BiliSettings(bili_db_dir=str(root))


async def test_sample_uid_sqlite_contract_reads_via_stdlib(tmp_path: Path) -> None:
    db_path = await _seed_sample_uid(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT bvid, title, transcription_status, transcription_source, "
            "transcript, audio_tokens, seconds FROM video_full WHERE bvid = ?",
            ("BVCONTRACT1",),
        ).fetchone()
        assert dict(row) == {
            "bvid": "BVCONTRACT1",
            "title": "contract video",
            "transcription_status": "success",
            "transcription_source": "mock",
            "transcript": "hello transcript",
            "audio_tokens": 11,
            "seconds": 120.0,
        }

        summary = conn.execute("SELECT * FROM manifest_summary").fetchone()
        assert int(summary["uid"]) == SAMPLE_UID
        assert summary["video_count"] == 1
        assert summary["transcribed_count"] == 1
        assert summary["total_audio_tokens"] == 11
        assert summary["total_audio_seconds"] == 120.0
        assert summary["total_cache_hits"] == 2

        latest = conn.execute(
            "SELECT bvid, title FROM video ORDER BY pubdate_ms DESC LIMIT 10",
        ).fetchall()
        assert [(r["bvid"], r["title"]) for r in latest] == [
            ("BVCONTRACT1", "contract video"),
        ]
    finally:
        conn.close()


async def test_schema_contract_tables_and_views_are_stable(tmp_path: Path) -> None:
    db_path = await _seed_sample_uid(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        objects = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')",
            )
        }
        for table in (
            "meta",
            "user_profile",
            "video",
            "video_page",
            "video_subtitle",
            "article",
            "opus_post",
            "dynamic_event",
            "audio_transcription",
            "image_asset",
        ):
            assert objects.get(table) == "table"
        assert objects.get("video_full") == "view"
        assert objects.get("manifest_summary") == "view"
    finally:
        conn.close()
