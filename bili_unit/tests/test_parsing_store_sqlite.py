# Contract tests for bili_unit.parsing._store.ParsingStore.
#
# Each save method round-trips a minimal dataclass (built via from_raw),
# then both the typed columns and the ``payload`` JSON are verified via
# direct SELECT against the main DB.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit.parsing._store import ParsingStore
from bili_unit.parsing.models.article import Article
from bili_unit.parsing.models.dynamic import DynamicPost
from bili_unit.parsing.models.opus import OpusPost
from bili_unit.parsing.models.up_profile import UpProfile
from bili_unit.parsing.models.video_detail import VideoDetail
from bili_unit.parsing.models.video_subtitle import VideoSubtitle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path: Path):
    ctx = UidContext(uid=42, root=tmp_path)
    await ctx.open()
    try:
        yield ParsingStore(ctx)
    finally:
        await ctx.close()


@pytest_asyncio.fixture
async def ctx(tmp_path: Path):
    ctx = UidContext(uid=42, root=tmp_path)
    await ctx.open()
    try:
        yield ctx
    finally:
        await ctx.close()


@pytest_asyncio.fixture
async def store_and_ctx(tmp_path: Path):
    """Yield (ParsingStore, UidContext) sharing the same connection."""
    c = UidContext(uid=42, root=tmp_path)
    await c.open()
    try:
        yield ParsingStore(c), c
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Raw fixture data — minimal but exercise every promoted column
# ---------------------------------------------------------------------------

def _up_profile() -> UpProfile:
    return UpProfile.from_raw(
        user_info={
            "mid": 42,
            "name": "tester",
            "sex": "保密",
            "sign": "hello, world",
            "face": "https://example.com/face.jpg",
            "birthday": "01-01",
            "level": 6,
            "jointime": 1500000000,
            "vip": {"type": 1, "status": 1, "label": {"text": "vip"}},
        },
        relation_info={"following": 100, "follower": 999, "whisper": 0, "black": 0},
        up_stat={"archive": {"view": 12345}, "article": {"view": 7}, "likes": 8},
    )


def _video() -> VideoDetail:
    raw = {
        "info": {
            "bvid": "BV1xx411c7mD",
            "aid": 1234567,
            "title": "测试视频",
            "desc": "描述",
            "duration": 600,
            "ctime": 1700000000,
            "pubdate": 1700000001,
            "pic": "https://example.com/cover.jpg",
            "pages": [
                {"cid": 111, "part": "P1", "duration": 300, "dimension": {}, "first_frame": ""},
                {"cid": 222, "part": "P2", "duration": 300, "dimension": {}, "first_frame": ""},
            ],
            "stat": {
                "view": 100, "danmaku": 50, "reply": 20, "favorite": 30,
                "coin": 10, "share": 5, "like": 80,
            },
            "owner": {"mid": 42, "name": "tester", "face": "f"},
            "rights": {}, "subtitle": {}, "label": {},
        },
        "tags": [{"tag_name": "tag1"}],
    }
    return VideoDetail.from_raw(raw)


def _video_subtitle_human() -> VideoSubtitle:
    """A subtitle where the resolved page is human-authored (zh-CN)."""
    raw = {
        "pages": [{"cid": 1, "part": "p1"}],
        "subtitle": [
            {
                "page_index": 0, "cid": 1, "part": "p1",
                "result": {},
                "content": [
                    {"lan": "zh-CN", "lan_doc": "中文",
                     "body": [{"from": 0.0, "to": 1.0, "content": "你好"}]},
                ],
            }
        ],
    }
    return VideoSubtitle.from_raw("BV_HUMAN", raw)


def _video_subtitle_ai() -> VideoSubtitle:
    """A subtitle where the resolved page is AI-only."""
    raw = {
        "pages": [{"cid": 1, "part": "p1"}],
        "subtitle": [
            {
                "page_index": 0, "cid": 1, "part": "p1",
                "result": {},
                "content": [
                    {"lan": "ai-zh", "lan_doc": "AI 中文",
                     "body": [{"from": 0.0, "to": 1.0, "content": "AI"}]},
                ],
            }
        ],
    }
    return VideoSubtitle.from_raw("BV_AI", raw)


def _article() -> Article:
    list_item = {
        "id": 99887766,
        "title": "Hello",
        "summary": "summary",
        "image_urls": ["https://example.com/a.jpg"],
        "stats": {"view": 1000, "favorite": 5, "like": 80, "reply": 7, "share": 1, "coin": 2},
        "ctime": 1600000000,
    }
    detail = {"markdown": "# body", "content_json": [{"type": "p", "content": "x"}]}
    return Article.from_raw(list_item, detail, [])


def _opus() -> OpusPost:
    list_item = {
        "opus_id": "opus_xyz",
        "title": "ttl",
        "summary": "smr",
        "cover": "https://example.com/c.jpg",
        "jump_url": "https://bilibili.com/opus/1",
        "stats": {"view": 1, "favorite": 0, "like": 0, "reply": 0, "share": 0, "coin": 0},
        "pub_time": 1700100000,
        "modules": {},
    }
    detail = {"markdown": "x", "images": [{"url": "https://example.com/i.jpg"}]}
    return OpusPost.from_raw(list_item, detail)


def _dynamic() -> DynamicPost:
    raw = {
        "id_str": "dyn_abcdef",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": 1700200000},
            "module_dynamic": {
                "desc": {"text": "hi"},
                "major": {
                    "type": "MAJOR_TYPE_DRAW",
                    "draw": {"items": [{"src": "https://example.com/d.jpg"}]},
                },
            },
        },
    }
    return DynamicPost.from_raw(raw)


# ---------------------------------------------------------------------------
# save_user_profile
# ---------------------------------------------------------------------------

async def test_save_user_profile_writes_typed_columns_and_payload(store_and_ctx):
    store, ctx = store_and_ctx
    profile = _up_profile()
    await store.save_user_profile(profile)

    row = await ctx.main.fetch_one(
        "SELECT uid, name, sign, face_url, level, follower, following, payload, parsed_at_ms "
        "FROM user_profile WHERE uid = ?",
        (42,),
    )
    assert row is not None
    assert row["uid"] == 42
    assert row["name"] == "tester"
    assert row["sign"] == "hello, world"
    assert row["face_url"] == "https://example.com/face.jpg"
    assert row["level"] == 6
    assert row["follower"] == 999
    assert row["following"] == 100
    assert row["parsed_at_ms"] > 0

    decoded = json.loads(row["payload"])
    assert decoded["mid"] == 42
    assert decoded == profile.to_dict()


async def test_save_user_profile_replaces_on_resave(store_and_ctx):
    store, ctx = store_and_ctx
    p1 = _up_profile()
    await store.save_user_profile(p1)

    p2 = _up_profile()
    p2.name = "renamed"
    await store.save_user_profile(p2)

    rows = await ctx.main.fetch_all("SELECT name FROM user_profile WHERE uid = 42")
    assert len(rows) == 1
    assert rows[0]["name"] == "renamed"


# ---------------------------------------------------------------------------
# save_video — typed columns, payload, video_page rows, transactional
# delete-then-insert of pages
# ---------------------------------------------------------------------------

async def test_save_video_writes_typed_columns_and_payload(store_and_ctx):
    store, ctx = store_and_ctx
    video = _video()
    await store.save_video(video)

    row = await ctx.main.fetch_one(
        "SELECT bvid, aid, title, description, cover_url, duration_s, pubdate_ms, "
        "       view_count, danmaku, reply, favorite, coin, share, like_count, "
        "       payload, parsed_at_ms "
        "  FROM video WHERE bvid = ?",
        (video.bvid,),
    )
    assert row is not None
    assert row["bvid"] == "BV1xx411c7mD"
    assert row["aid"] == 1234567
    assert row["title"] == "测试视频"
    assert row["description"] == "描述"
    assert row["cover_url"] == "https://example.com/cover.jpg"
    assert row["duration_s"] == 600
    assert row["pubdate_ms"] == 1700000001 * 1000
    assert row["view_count"] == 100
    assert row["danmaku"] == 50
    assert row["reply"] == 20
    assert row["favorite"] == 30
    assert row["coin"] == 10
    assert row["share"] == 5
    assert row["like_count"] == 80

    decoded = json.loads(row["payload"])
    assert decoded == video.to_dict()


async def test_save_video_writes_video_page_rows(store_and_ctx):
    store, ctx = store_and_ctx
    video = _video()
    await store.save_video(video)

    pages = await ctx.main.fetch_all(
        "SELECT page_no, cid, part, duration_s "
        "  FROM video_page WHERE bvid = ? ORDER BY page_no",
        (video.bvid,),
    )
    assert len(pages) == 2
    assert pages[0]["page_no"] == 1
    assert pages[0]["cid"] == 111
    assert pages[0]["part"] == "P1"
    assert pages[0]["duration_s"] == 300
    assert pages[1]["page_no"] == 2
    assert pages[1]["cid"] == 222


async def test_save_video_replaces_pages_on_resave(store_and_ctx):
    store, ctx = store_and_ctx
    v1 = _video()
    await store.save_video(v1)

    # Re-parse with fewer pages.
    v2 = _video()
    v2.pages = v2.pages[:1]
    await store.save_video(v2)

    page_rows = await ctx.main.fetch_all(
        "SELECT page_no FROM video_page WHERE bvid = ? ORDER BY page_no",
        (v2.bvid,),
    )
    assert [r["page_no"] for r in page_rows] == [1]


async def test_save_video_preserves_child_rows_on_resave(store_and_ctx):
    store, ctx = store_and_ctx
    video = _video()
    await store.save_video(video)
    await store.save_video_subtitle(_video_subtitle_for_existing_video())
    await ctx.main.execute(
        """
        INSERT INTO audio_transcription(
            bvid, status, transcription_source, transcript, audio_tokens,
            seconds, cache_hits, payload, processed_at_ms
        ) VALUES (?, 'success', 'asr', 'ok', 1, 1.0, 0, '{}', 1)
        """,
        (video.bvid,),
    )
    await ctx.main.execute(
        """
        INSERT INTO audio_transcription_page(
            bvid, page_no, page_index, cid, duration_s, language, asr_model,
            transcript_text, transcript_char_count, segment_count
        ) VALUES (?, 1, 0, 111, 1.0, 'zh', 'mimo', 'ok', 2, 1)
        """,
        (video.bvid,),
    )
    await ctx.main.execute(
        """
        INSERT INTO audio_transcription_segment(
            bvid, page_no, segment_no, start_seconds, end_seconds,
            duration_s, transcript_text, language, asr_model
        ) VALUES (?, 1, 1, 0.0, 1.0, 1.0, 'ok', 'zh', 'mimo')
        """,
        (video.bvid,),
    )

    video.title = "resaved"
    await store.save_video(video)

    subtitle_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM video_subtitle WHERE bvid = ?",
        (video.bvid,),
    )
    audio_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription WHERE bvid = ?",
        (video.bvid,),
    )
    subtitle_page_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM video_subtitle_page WHERE bvid = ?",
        (video.bvid,),
    )
    subtitle_segment_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM video_subtitle_segment WHERE bvid = ?",
        (video.bvid,),
    )
    audio_page_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription_page WHERE bvid = ?",
        (video.bvid,),
    )
    audio_segment_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription_segment WHERE bvid = ?",
        (video.bvid,),
    )
    assert subtitle_count == 1
    assert audio_count == 1
    assert subtitle_page_count == 1
    assert subtitle_segment_count == 1
    assert audio_page_count == 1
    assert audio_segment_count == 1


# ---------------------------------------------------------------------------
# save_video_subtitle — derived Bilibili subtitle source flags
# ---------------------------------------------------------------------------

async def test_save_video_subtitle_human_only(store_and_ctx):
    store, ctx = store_and_ctx
    sub = _video_subtitle_human()
    # Need a parent video row because of the FK.
    await store.save_video(_with_bvid(_video(), "BV_HUMAN"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT bvid, "
        "has_bilibili_human_uploaded_or_official_subtitle, "
        "has_bilibili_platform_ai_generated_subtitle, payload "
        "FROM video_subtitle WHERE bvid = ?",
        ("BV_HUMAN",),
    )
    assert row is not None
    assert row["has_bilibili_human_uploaded_or_official_subtitle"] == 1
    assert row["has_bilibili_platform_ai_generated_subtitle"] == 0
    decoded = json.loads(row["payload"])
    assert decoded == sub.to_dict()
    page = sub.pages[0]
    pages = await ctx.main.fetch_all(
        "SELECT page_no, bilibili_video_page_index, bilibili_video_page_cid, "
        "       selected_bilibili_subtitle_language_code, "
        "       is_selected_bilibili_subtitle_platform_ai_generated, "
        "       selected_bilibili_subtitle_text, subtitle_segment_count "
        "FROM video_subtitle_page WHERE bvid = ?",
        ("BV_HUMAN",),
    )
    assert [dict(r) for r in pages] == [
        {
            "page_no": 1,
            "bilibili_video_page_index": page.page_index,
            "bilibili_video_page_cid": page.cid,
            "selected_bilibili_subtitle_language_code": page.lan,
            "is_selected_bilibili_subtitle_platform_ai_generated": int(page.is_ai),
            "selected_bilibili_subtitle_text": " ".join(
                segment.content for segment in page.segments
            ),
            "subtitle_segment_count": len(page.segments),
        },
    ]
    segment = page.segments[0]
    segments = await ctx.main.fetch_all(
        "SELECT page_no, segment_no, bilibili_subtitle_start_seconds, "
        "       bilibili_subtitle_end_seconds, "
        "       bilibili_subtitle_duration_seconds, "
        "       bilibili_subtitle_segment_text "
        "FROM video_subtitle_segment WHERE bvid = ?",
        ("BV_HUMAN",),
    )
    assert [dict(r) for r in segments] == [
        {
            "page_no": 1,
            "segment_no": 1,
            "bilibili_subtitle_start_seconds": segment.start,
            "bilibili_subtitle_end_seconds": segment.end,
            "bilibili_subtitle_duration_seconds": segment.end - segment.start,
            "bilibili_subtitle_segment_text": segment.content,
        },
    ]


async def test_save_video_subtitle_ai_only(store_and_ctx):
    store, ctx = store_and_ctx
    sub = _video_subtitle_ai()
    await store.save_video(_with_bvid(_video(), "BV_AI"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT has_bilibili_human_uploaded_or_official_subtitle, "
        "has_bilibili_platform_ai_generated_subtitle, payload "
        "FROM video_subtitle WHERE bvid = ?",
        ("BV_AI",),
    )
    assert row is not None
    assert row["has_bilibili_human_uploaded_or_official_subtitle"] == 0
    assert row["has_bilibili_platform_ai_generated_subtitle"] == 1
    decoded = json.loads(row["payload"])
    assert decoded == sub.to_dict()


async def test_save_video_subtitle_empty_result_keeps_main_row(store_and_ctx):
    store, ctx = store_and_ctx
    sub = VideoSubtitle.from_raw(
        "BV_EMPTY_SUB",
        {
            "subtitle": [
                {
                    "page_index": 0,
                    "cid": 1,
                    "content": [],
                },
            ],
        },
    )
    await store.save_video(_with_bvid(_video(), "BV_EMPTY_SUB"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT has_bilibili_human_uploaded_or_official_subtitle, "
        "has_bilibili_platform_ai_generated_subtitle, payload "
        "FROM video_subtitle WHERE bvid = ?",
        ("BV_EMPTY_SUB",),
    )
    assert row is not None
    assert row["has_bilibili_human_uploaded_or_official_subtitle"] == 0
    assert row["has_bilibili_platform_ai_generated_subtitle"] == 0
    decoded = json.loads(row["payload"])
    assert decoded["bilibili_subtitle_pages"] == []

    page_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM video_subtitle_page WHERE bvid = ?",
        ("BV_EMPTY_SUB",),
    )
    segment_count = await ctx.main.fetch_value(
        "SELECT COUNT(*) FROM video_subtitle_segment WHERE bvid = ?",
        ("BV_EMPTY_SUB",),
    )
    assert page_count == 0
    assert segment_count == 0


async def test_save_video_subtitle_keeps_mixed_ai_pages(store_and_ctx):
    store, ctx = store_and_ctx
    raw = {
        "subtitle": [
            {
                "page_index": 0,
                "cid": 1,
                "content": [
                    {
                        "lan": "zh-CN",
                        "lan_doc": "Chinese",
                        "body": [{"from": 0.0, "to": 1.0, "content": "human"}],
                    },
                ],
            },
            {
                "page_index": 1,
                "cid": 2,
                "content": [
                    {
                        "lan": "ai-zh",
                        "lan_doc": "AI Chinese",
                        "body": [{"from": 0.0, "to": 1.0, "content": "ai"}],
                    },
                ],
            },
        ],
    }
    sub = VideoSubtitle.from_raw("BV_MIXED", raw)
    await store.save_video(_with_bvid(_video(), "BV_MIXED"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT has_bilibili_human_uploaded_or_official_subtitle, "
        "has_bilibili_platform_ai_generated_subtitle, payload "
        "FROM video_subtitle WHERE bvid = ?",
        ("BV_MIXED",),
    )
    assert row is not None
    assert row["has_bilibili_human_uploaded_or_official_subtitle"] == 1
    assert row["has_bilibili_platform_ai_generated_subtitle"] == 1
    decoded = json.loads(row["payload"])
    assert [
        p["selected_bilibili_subtitle_language_code"]
        for p in decoded["bilibili_subtitle_pages"]
    ] == ["zh-CN", "ai-zh"]
    assert decoded["available_bilibili_subtitle_language_codes"] == [
        "zh-CN", "ai-zh",
    ]
    page_flags = await ctx.main.fetch_all(
        "SELECT page_no, selected_bilibili_subtitle_language_code, "
        "       is_selected_bilibili_subtitle_platform_ai_generated "
        "FROM video_subtitle_page WHERE bvid = ? ORDER BY page_no",
        ("BV_MIXED",),
    )
    assert [dict(r) for r in page_flags] == [
        {
            "page_no": 1,
            "selected_bilibili_subtitle_language_code": "zh-CN",
            "is_selected_bilibili_subtitle_platform_ai_generated": 0,
        },
        {
            "page_no": 2,
            "selected_bilibili_subtitle_language_code": "ai-zh",
            "is_selected_bilibili_subtitle_platform_ai_generated": 1,
        },
    ]


async def test_save_video_subtitle_marks_ai_available_language(store_and_ctx):
    store, ctx = store_and_ctx
    raw = {
        "subtitle": [
            {
                "page_index": 0,
                "cid": 1,
                "content": [
                    {
                        "lan": "zh-CN",
                        "lan_doc": "Chinese",
                        "body": [{"from": 0.0, "to": 1.0, "content": "human"}],
                    },
                    {
                        "lan": "ai-zh",
                        "lan_doc": "AI Chinese",
                        "body": [{"from": 0.0, "to": 1.0, "content": "ai"}],
                    },
                ],
            },
        ],
    }
    sub = VideoSubtitle.from_raw("BV_BOTH_LANGS", raw)
    await store.save_video(_with_bvid(_video(), "BV_BOTH_LANGS"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT has_bilibili_human_uploaded_or_official_subtitle, "
        "has_bilibili_platform_ai_generated_subtitle, payload "
        "FROM video_subtitle WHERE bvid = ?",
        ("BV_BOTH_LANGS",),
    )
    assert row is not None
    assert row["has_bilibili_human_uploaded_or_official_subtitle"] == 1
    assert row["has_bilibili_platform_ai_generated_subtitle"] == 1
    decoded = json.loads(row["payload"])
    page = decoded["bilibili_subtitle_pages"][0]
    assert page["selected_bilibili_subtitle_language_code"] == "zh-CN"
    assert page["is_selected_bilibili_subtitle_platform_ai_generated"] is False
    assert decoded["available_bilibili_subtitle_language_codes"] == [
        "zh-CN", "ai-zh",
    ]


async def test_save_video_subtitle_resave_rebuilds_materialized_rows(store_and_ctx):
    store, ctx = store_and_ctx
    raw = {
        "subtitle": [
            {
                "page_index": 0,
                "cid": 1,
                "content": [
                    {
                        "lan": "zh-CN",
                        "body": [{"from": 0.0, "to": 1.0, "content": "old-1"}],
                    },
                ],
            },
            {
                "page_index": 1,
                "cid": 2,
                "content": [
                    {
                        "lan": "zh-CN",
                        "body": [{"from": 0.0, "to": 1.0, "content": "old-2"}],
                    },
                ],
            },
        ],
    }
    sub = VideoSubtitle.from_raw("BV_REWRITE_SUB", raw)
    await store.save_video(_with_bvid(_video(), "BV_REWRITE_SUB"))
    await store.save_video_subtitle(sub)

    sub.pages = sub.pages[:1]
    sub.pages[0].segments = [
        sub.pages[0].segments[0],
    ]
    sub.pages[0].segments[0].content = "new"
    await store.save_video_subtitle(sub)

    pages = await ctx.main.fetch_all(
        "SELECT page_no, selected_bilibili_subtitle_text "
        "FROM video_subtitle_page WHERE bvid = ? ORDER BY page_no",
        ("BV_REWRITE_SUB",),
    )
    assert [dict(r) for r in pages] == [
        {"page_no": 1, "selected_bilibili_subtitle_text": "new"},
    ]
    segments = await ctx.main.fetch_all(
        "SELECT page_no, segment_no, bilibili_subtitle_segment_text "
        "FROM video_subtitle_segment WHERE bvid = ? ORDER BY page_no, segment_no",
        ("BV_REWRITE_SUB",),
    )
    assert [dict(r) for r in segments] == [
        {
            "page_no": 1,
            "segment_no": 1,
            "bilibili_subtitle_segment_text": "new",
        },
    ]


def _with_bvid(video: VideoDetail, bvid: str) -> VideoDetail:
    """Helper: clone a VideoDetail and override its bvid."""
    video.bvid = bvid
    return video


# ---------------------------------------------------------------------------
# save_article
# ---------------------------------------------------------------------------

async def test_save_article_writes_typed_columns_and_payload(store_and_ctx):
    store, ctx = store_and_ctx
    art = _article()
    await store.save_article(art)

    row = await ctx.main.fetch_one(
        "SELECT cvid, title, summary, pubdate_ms, view_count, like_count, reply, payload "
        "  FROM article WHERE cvid = ?",
        (art.id,),
    )
    assert row is not None
    assert row["cvid"] == "99887766"
    assert row["title"] == "Hello"
    assert row["summary"] == "summary"
    assert row["pubdate_ms"] == 1600000000 * 1000
    assert row["view_count"] == 1000
    assert row["like_count"] == 80
    assert row["reply"] == 7
    decoded = json.loads(row["payload"])
    assert decoded == art.to_dict()


# ---------------------------------------------------------------------------
# save_opus
# ---------------------------------------------------------------------------

async def test_save_opus_writes_typed_columns_and_payload(store_and_ctx):
    store, ctx = store_and_ctx
    opus = _opus()
    await store.save_opus(opus)

    row = await ctx.main.fetch_one(
        "SELECT opus_id, pubdate_ms, payload FROM opus_post WHERE opus_id = ?",
        ("opus_xyz",),
    )
    assert row is not None
    assert row["opus_id"] == "opus_xyz"
    assert row["pubdate_ms"] == 1700100000 * 1000
    decoded = json.loads(row["payload"])
    assert decoded == opus.to_dict()


# ---------------------------------------------------------------------------
# save_dynamic
# ---------------------------------------------------------------------------

async def test_save_dynamic_writes_typed_columns_and_payload(store_and_ctx):
    store, ctx = store_and_ctx
    dyn = _dynamic()
    await store.save_dynamic(dyn)

    row = await ctx.main.fetch_one(
        "SELECT dynamic_id, type, pubdate_ms, payload FROM dynamic_event WHERE dynamic_id = ?",
        (dyn.dynamic_id,),
    )
    assert row is not None
    assert row["dynamic_id"] == "dyn_abcdef"
    assert row["type"] == "DYNAMIC_TYPE_DRAW"
    assert row["pubdate_ms"] == 1700200000 * 1000
    decoded = json.loads(row["payload"])
    assert decoded == dyn.to_dict()


# ---------------------------------------------------------------------------
# save_image_asset
# ---------------------------------------------------------------------------

async def test_save_image_asset_inserts_row(store_and_ctx):
    store, ctx = store_and_ctx
    url = "https://example.com/face.jpg"
    data = b"fake-image"
    await store.save_image_asset(
        url=url,
        source_kind="profile.face",
        source_id="42",
        file_path="avatar.jpg",
        bytes=len(data),
        data=data,
        status="ok",
    )

    expected_hash = hashlib.md5(url.encode("utf-8"), usedforsecurity=False).hexdigest()
    row = await ctx.main.fetch_one(
        "SELECT * FROM image_asset WHERE url_hash = ?",
        (expected_hash,),
    )
    assert row is not None
    assert row["source_kind"] == "profile.face"
    assert row["source_id"] == "42"
    assert row["url"] == url
    assert row["file_path"] == "avatar.jpg"
    assert row["bytes"] == len(data)
    assert row["data"] == data
    assert row["status"] == "ok"
    assert row["downloaded_at_ms"] > 0

    asset = await store.get_image_asset(url)
    assert asset is not None
    assert asset["data"] == data


async def test_save_image_asset_upserts_on_same_url(store_and_ctx):
    store, ctx = store_and_ctx
    url = "https://example.com/face.jpg"

    await store.save_image_asset(
        url=url, source_kind="profile.face", source_id="42",
        file_path=None, bytes=None, status="failed",
    )
    await store.save_image_asset(
        url=url, source_kind="profile.face", source_id="42",
        file_path="avatar.jpg", bytes=3, data=b"new", status="ok",
    )

    rows = await ctx.main.fetch_all("SELECT status, bytes, data FROM image_asset")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["bytes"] == 3
    assert rows[0]["data"] == b"new"


async def test_update_model_payload_preserves_parsed_at_ms(store_and_ctx):
    store, ctx = store_and_ctx
    video = _video()
    await store.save_video(video)
    before = await ctx.main.fetch_one(
        "SELECT payload, parsed_at_ms FROM video WHERE bvid = ?",
        (video.bvid,),
    )
    assert before is not None
    updated_payload = json.loads(before["payload"])
    updated_payload["cover_local"] = "video/cover.jpg"

    await store.update_model_payload("video_work", video.bvid, updated_payload)

    after = await ctx.main.fetch_one(
        "SELECT payload, parsed_at_ms FROM video WHERE bvid = ?",
        (video.bvid,),
    )
    assert after is not None
    assert json.loads(after["payload"])["cover_local"] == "video/cover.jpg"
    assert after["parsed_at_ms"] == before["parsed_at_ms"]


async def test_list_image_assets_returns_dict_rows(store_and_ctx):
    store, _ = store_and_ctx
    await store.save_image_asset(
        url="https://example.com/a.jpg",
        source_kind="opus.image", source_id="opus_1",
        file_path="opus/1.jpg", bytes=100, status="ok",
    )
    await store.save_image_asset(
        url="https://example.com/b.jpg",
        source_kind="video.cover", source_id="BV1",
        file_path="video/cover.jpg", bytes=200, status="ok",
    )
    out = await store.list_image_assets()
    assert len(out) == 2
    assert {r["url"] for r in out} == {
        "https://example.com/a.jpg", "https://example.com/b.jpg",
    }
    for r in out:
        assert {"url_hash", "source_kind", "source_id", "url",
                "file_path", "bytes", "status", "downloaded_at_ms"} <= r.keys()
        assert "data" not in r


# ---------------------------------------------------------------------------
# get_existing_item_ids — every model variant
# ---------------------------------------------------------------------------

async def test_get_existing_item_ids_per_model(store):
    # Empty up front
    for m in ("user_profile", "video_work", "video_subtitle",
              "article_post", "opus_post", "dynamic_event"):
        assert await store.get_existing_item_ids(m) == set()

    await store.save_user_profile(_up_profile())
    await store.save_video(_video())
    await store.save_video_subtitle(_video_subtitle_for_existing_video())
    await store.save_article(_article())
    await store.save_opus(_opus())
    await store.save_dynamic(_dynamic())

    assert await store.get_existing_item_ids("user_profile") == {"42"}
    assert await store.get_existing_item_ids("video_work") == {"BV1xx411c7mD"}
    assert await store.get_existing_item_ids("video_subtitle") == {"BV1xx411c7mD"}
    assert await store.get_existing_item_ids("article_post") == {"99887766"}
    assert await store.get_existing_item_ids("opus_post") == {"opus_xyz"}
    assert await store.get_existing_item_ids("dynamic_event") == {"dyn_abcdef"}


async def test_get_item_parsed_at_ms_returns_timestamps(store):
    await store.save_user_profile(_up_profile())
    await store.save_video(_video())
    await store.save_video_subtitle(_video_subtitle_for_existing_video())
    await store.save_article(_article())
    await store.save_opus(_opus())
    await store.save_dynamic(_dynamic())

    assert await store.get_item_parsed_at_ms("user_profile", "42") is not None
    assert await store.get_item_parsed_at_ms(
        "video_work", "BV1xx411c7mD",
    ) is not None
    assert await store.get_item_parsed_at_ms(
        "video_subtitle", "BV1xx411c7mD",
    ) is not None
    assert await store.get_item_parsed_at_ms(
        "article_post", "99887766",
    ) is not None
    assert await store.get_item_parsed_at_ms("opus_post", "opus_xyz") is not None
    assert await store.get_item_parsed_at_ms(
        "dynamic_event", "dyn_abcdef",
    ) is not None
    assert await store.get_item_parsed_at_ms("video_work", "missing") is None
    with pytest.raises(ValueError):
        await store.get_item_parsed_at_ms("unknown", "id")


def _video_subtitle_for_existing_video() -> VideoSubtitle:
    """Subtitle with bvid matching the saved video — keeps FK satisfied."""
    raw = {
        "pages": [{"cid": 1, "part": "p1"}],
        "subtitle": [
            {
                "page_index": 0, "cid": 1, "part": "p1",
                "result": {},
                "content": [
                    {"lan": "zh-CN", "lan_doc": "中文",
                     "body": [{"from": 0.0, "to": 1.0, "content": "你好"}]},
                ],
            }
        ],
    }
    return VideoSubtitle.from_raw("BV1xx411c7mD", raw)


# ---------------------------------------------------------------------------
# get_*_payload round-trip
# ---------------------------------------------------------------------------

async def test_get_video_payload_round_trips(store):
    video = _video()
    await store.save_video(video)
    out = await store.get_video_payload(video.bvid)
    assert out == video.to_dict()


async def test_get_video_payload_missing_returns_none(store):
    assert await store.get_video_payload("BV_NONEXIST") is None


async def test_get_user_profile_payload_round_trips(store):
    p = _up_profile()
    await store.save_user_profile(p)
    out = await store.get_user_profile_payload(42)
    assert out == p.to_dict()


async def test_get_article_payload_round_trips(store):
    a = _article()
    await store.save_article(a)
    assert await store.get_article_payload(a.id) == a.to_dict()


async def test_get_opus_payload_round_trips(store):
    o = _opus()
    await store.save_opus(o)
    assert await store.get_opus_payload(o.id) == o.to_dict()


async def test_get_dynamic_payload_round_trips(store):
    d = _dynamic()
    await store.save_dynamic(d)
    assert await store.get_dynamic_payload(d.dynamic_id) == d.to_dict()


# ---------------------------------------------------------------------------
# Task state
# ---------------------------------------------------------------------------

_MODELS = [
    "user_profile", "video_work", "video_subtitle",
    "article_post", "opus_post", "dynamic_event",
]


async def test_init_task_creates_pending_entries(store_and_ctx):
    store, ctx = store_and_ctx
    await store.init_task(_MODELS)

    payload = await store.get_task()
    assert payload is not None
    for m in _MODELS:
        assert payload["models"][m] == {"status": "PENDING", "count": 0}
    assert payload["images"] is None

    row = await ctx.main.fetch_one(
        "SELECT stage, status, created_at_ms, updated_at_ms FROM stage_task WHERE stage='parsing'",
    )
    assert row is not None
    assert row["stage"] == "parsing"
    assert row["status"] == "PENDING"


async def test_init_task_idempotent_does_not_reset_statuses(store):
    await store.init_task(_MODELS)
    await store.update_task_model_status("video_work", "SUCCESS", count=7)

    # Second init does NOT clobber the SUCCESS / 7 entry.
    await store.init_task(_MODELS)
    payload = await store.get_task()
    assert payload["models"]["video_work"] == {"status": "SUCCESS", "count": 7}
    # Untouched models stay PENDING.
    assert payload["models"]["article_post"] == {"status": "PENDING", "count": 0}


async def test_update_task_model_status_only_touches_target_model(store):
    await store.init_task(_MODELS)
    await store.update_task_model_status("article_post", "SUCCESS", count=12)

    payload = await store.get_task()
    assert payload["models"]["article_post"] == {"status": "SUCCESS", "count": 12}
    # Others remain PENDING.
    for m in _MODELS:
        if m == "article_post":
            continue
        assert payload["models"][m] == {"status": "PENDING", "count": 0}


async def test_update_task_images_writes_block(store):
    await store.init_task(_MODELS)
    summary = {
        "total": 5, "ok": 3, "skipped": 1, "failed": 1,
        "failed_urls": ["https://example.com/x.jpg"],
    }
    await store.update_task_images(summary)

    payload = await store.get_task()
    assert payload["images"] == summary
    # Models block survives.
    assert "user_profile" in payload["models"]


async def test_update_task_status_changes_status_column(store_and_ctx):
    store, ctx = store_and_ctx
    await store.init_task(_MODELS)
    await store.update_task_status("SUCCESS")

    status = await ctx.main.fetch_value(
        "SELECT status FROM stage_task WHERE stage = 'parsing'",
    )
    assert status == "SUCCESS"


async def test_get_task_returns_none_when_uninitialised(store):
    assert await store.get_task() is None
