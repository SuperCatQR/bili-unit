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
            "bvid, has_bilibili_human_uploaded_or_official_subtitle, "
            "has_bilibili_platform_ai_generated_subtitle, payload, parsed_at_ms"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                0,
                json.dumps({
                    "bvid": "BVCONTRACT1",
                    "bilibili_subtitle_pages": [],
                }),
                1002,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO video_subtitle_page("
            "bvid, page_no, bilibili_video_page_index, bilibili_video_page_cid, "
            "selected_bilibili_subtitle_language_code, "
            "selected_bilibili_subtitle_language_name, "
            "is_selected_bilibili_subtitle_platform_ai_generated, "
            "selected_bilibili_subtitle_text, subtitle_segment_count, parsed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                0,
                12345,
                "zh-CN",
                "Chinese",
                0,
                "hello subtitle",
                1,
                1002,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO video_subtitle_segment("
            "bvid, page_no, segment_no, bilibili_subtitle_start_seconds, "
            "bilibili_subtitle_end_seconds, bilibili_subtitle_duration_seconds, "
            "bilibili_subtitle_segment_text"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("BVCONTRACT1", 1, 1, 0.0, 1.0, 1.0, "hello subtitle"),
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
            "INSERT OR REPLACE INTO audio_transcription_page("
            "bvid, page_no, page_index, cid, duration_s, language, asr_model, "
            "transcript_text, transcript_char_count, segment_count"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                0,
                12345,
                120.0,
                "zh",
                "mimo-v2.5-asr",
                "hello transcript",
                len("hello transcript"),
                1,
            ),
        )
        await ctx.main.execute(
            "INSERT OR REPLACE INTO audio_transcription_segment("
            "bvid, page_no, segment_no, start_seconds, end_seconds, "
            "duration_s, transcript_text, language, asr_model, "
            "is_empty_transcript_skip, is_high_risk_audio_skip, error_message"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BVCONTRACT1",
                1,
                1,
                0.0,
                1.0,
                1.0,
                "hello transcript",
                "zh",
                "mimo-v2.5-asr",
                0,
                0,
                None,
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

        subtitle_page = conn.execute(
            "SELECT selected_bilibili_subtitle_language_code, "
            "is_selected_bilibili_subtitle_platform_ai_generated, "
            "selected_bilibili_subtitle_text "
            "FROM video_subtitle_page WHERE bvid = ? AND page_no = 1",
            ("BVCONTRACT1",),
        ).fetchone()
        assert dict(subtitle_page) == {
            "selected_bilibili_subtitle_language_code": "zh-CN",
            "is_selected_bilibili_subtitle_platform_ai_generated": 0,
            "selected_bilibili_subtitle_text": "hello subtitle",
        }

        subtitle_segments = conn.execute(
            "SELECT bilibili_subtitle_duration_seconds, "
            "bilibili_subtitle_segment_text "
            "FROM video_subtitle_segment WHERE bvid = ? ORDER BY segment_no",
            ("BVCONTRACT1",),
        ).fetchall()
        assert [dict(r) for r in subtitle_segments] == [{
            "bilibili_subtitle_duration_seconds": 1.0,
            "bilibili_subtitle_segment_text": "hello subtitle",
        }]

        asr_page = conn.execute(
            "SELECT language, asr_model, transcript_text, segment_count "
            "FROM audio_transcription_page WHERE bvid = ? AND page_no = 1",
            ("BVCONTRACT1",),
        ).fetchone()
        assert dict(asr_page) == {
            "language": "zh",
            "asr_model": "mimo-v2.5-asr",
            "transcript_text": "hello transcript",
            "segment_count": 1,
        }

        asr_segments = conn.execute(
            "SELECT transcript_text, is_empty_transcript_skip, "
            "is_high_risk_audio_skip, error_message "
            "FROM audio_transcription_segment "
            "WHERE bvid = ? ORDER BY segment_no",
            ("BVCONTRACT1",),
        ).fetchall()
        assert [dict(r) for r in asr_segments] == [{
            "transcript_text": "hello transcript",
            "is_empty_transcript_skip": 0,
            "is_high_risk_audio_skip": 0,
            "error_message": None,
        }]
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
            "video_subtitle_page",
            "video_subtitle_segment",
            "article",
            "opus_post",
            "dynamic_event",
            "audio_transcription",
            "audio_transcription_page",
            "audio_transcription_segment",
            "image_asset",
        ):
            assert objects.get(table) == "table"
        assert objects.get("video_full") == "view"
        assert objects.get("manifest_summary") == "view"
    finally:
        conn.close()
