# Contract tests for bili_unit.parsing._store.ParsingStore.
#
# Each save method round-trips a minimal dataclass (built via from_raw),
# then both the typed columns and the ``payload`` JSON are verified via
# direct SELECT against the main DB.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


# ---------------------------------------------------------------------------
# save_video_subtitle — derived has_official / has_ai flags
# ---------------------------------------------------------------------------

async def test_save_video_subtitle_human_only(store_and_ctx):
    store, ctx = store_and_ctx
    sub = _video_subtitle_human()
    # Need a parent video row because of the FK.
    await store.save_video(_with_bvid(_video(), "BV_HUMAN"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT bvid, has_official, has_ai, payload FROM video_subtitle WHERE bvid = ?",
        ("BV_HUMAN",),
    )
    assert row is not None
    assert row["has_official"] == 1
    assert row["has_ai"] == 0
    decoded = json.loads(row["payload"])
    assert decoded == sub.to_dict()


async def test_save_video_subtitle_ai_only(store_and_ctx):
    store, ctx = store_and_ctx
    sub = _video_subtitle_ai()
    await store.save_video(_with_bvid(_video(), "BV_AI"))
    await store.save_video_subtitle(sub)

    row = await ctx.main.fetch_one(
        "SELECT has_official, has_ai FROM video_subtitle WHERE bvid = ?",
        ("BV_AI",),
    )
    assert row["has_official"] == 0
    assert row["has_ai"] == 1


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
    await store.save_image_asset(
        url=url,
        source_kind="profile.face",
        source_id="42",
        file_path="avatar.jpg",
        bytes=12345,
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
    assert row["bytes"] == 12345
    assert row["status"] == "ok"
    assert row["downloaded_at_ms"] > 0


async def test_save_image_asset_upserts_on_same_url(store_and_ctx):
    store, ctx = store_and_ctx
    url = "https://example.com/face.jpg"

    await store.save_image_asset(
        url=url, source_kind="profile.face", source_id="42",
        file_path=None, bytes=None, status="failed",
    )
    await store.save_image_asset(
        url=url, source_kind="profile.face", source_id="42",
        file_path="avatar.jpg", bytes=999, status="ok",
    )

    rows = await ctx.main.fetch_all("SELECT status, bytes FROM image_asset")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["bytes"] == 999


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
