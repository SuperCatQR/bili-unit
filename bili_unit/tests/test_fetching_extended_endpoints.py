# tests for the expanded fetching endpoint surface (Phase 6 rewrite).
#
# Catalog / adapter / pagination tests are unchanged in spirit; the legacy
# Query + _item_fetch_key writer probe is replaced with direct SQL inspection
# of the new SQLite raw_payload table.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import EndpointStatus
from bili_unit.fetching._bilibili_adapter import (
    FetchPageResult,
    fetch_endpoint,
)
from bili_unit.fetching._endpoint_catalog import ENDPOINTS, get_endpoint
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.runner import Runner

USER_EXTENDED_ENDPOINTS = {
    "access_id",
    "channels",
    "media_list",
    "live_info",
    "user_relation",
    "reservation",
    "uplikeimg",
    "top_followers",
    "followings",
    "followers",
    "same_followers",
}


VIDEO_EXTENDED_ENDPOINTS = {
    "video_pages",
    "video_detail_full",
    "video_ai_conclusion",
    "video_danmaku_snapshot",
    "video_danmaku_view",
    "video_danmaku_xml",
    "video_danmakus",
    "video_online",
    "video_pay_coins",
    "video_pbp",
    "video_player_info",
    "video_private_notes",
    "video_public_notes",
    "video_related",
    "video_relation",
    "video_special_dms",
    "video_subtitle",
    "video_up_mid",
    "video_snapshot",
    "video_download_url",
    "video_is_episode",
    "video_is_forbid_note",
    "video_chargers",
}


def _settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(bili_db_dir=str(tmp_path))


def _rate_limit() -> RateLimitController:
    return RateLimitController(
        global_qps=1000.0,
        endpoint_qps=1000.0,
        video_detail_qps=1000.0,
        pause_seconds=0,
    )


# ---------------------------------------------------------------------------
# catalog static checks
# ---------------------------------------------------------------------------

def test_extended_endpoint_surface_registered():
    names = {ep.name for ep in ENDPOINTS}
    assert names >= USER_EXTENDED_ENDPOINTS
    assert names >= VIDEO_EXTENDED_ENDPOINTS
    assert "upower_qa_detail" in names


def test_video_extended_endpoints_are_item_fanouts():
    for name in VIDEO_EXTENDED_ENDPOINTS:
        ep = get_endpoint(name)
        assert ep is not None, name
        assert ep.kind == "item", name
        assert ep.source_endpoint == "videos", name
        assert ep.extract_items is not None, name


def test_upower_qa_detail_fanout_registered():
    ep = get_endpoint("upower_qa_detail")
    assert ep is not None
    assert ep.kind == "item"
    assert ep.source_endpoint == "upower_qa"
    assert ep.needs_parent_uid is True
    assert ep.extract_items is not None


# ---------------------------------------------------------------------------
# fetch_endpoint pagination strategies
# ---------------------------------------------------------------------------

async def test_fetch_endpoint_oid_pagination():
    spec = get_endpoint("media_list")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        oid = kw.get("oid")
        if oid is None:
            return {
                "media_list": [{"aid": 10, "bvid": "BV10"}, {"aid": 9, "bvid": "BV9"}],
                "total": 3,
            }
        return {
            "media_list": [{"aid": 8, "bvid": "BV8"}],
            "total": 3,
        }

    with patch.object(spec, "callable", fake_call):
        r1 = await fetch_endpoint(1, spec, None, {"oid": None, "ps": 2})
        assert not r1.is_last_page
        assert r1.next_request == {"oid": 9, "ps": 2}

        r2 = await fetch_endpoint(1, spec, None, r1.next_request)
        assert r2.is_last_page


# ---------------------------------------------------------------------------
# per-page helper plumbing (subtitle / danmakus serialisation)
# ---------------------------------------------------------------------------

async def test_video_per_page_helper_serialises_pages():
    spec = get_endpoint("video_subtitle")
    assert spec is not None

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[
            {"cid": 111, "part": "p1"},
            {"cid": 222, "part": "p2"},
        ])
        instance.get_subtitle = AsyncMock(side_effect=[
            {"subtitles": [{"id": 1}]},
            {"subtitles": [{"id": 2}]},
        ])

        result = await spec.callable("BV1", None)

    assert [row["cid"] for row in result["subtitle"]] == [111, 222]
    assert result["subtitle"][0]["result"]["subtitles"][0]["id"] == 1
    assert result["pages"][1]["part"] == "p2"


async def test_video_danmaku_objects_are_json_safe():
    from bilibili_api.utils.danmaku import Danmaku

    spec = get_endpoint("video_danmakus")
    assert spec is not None

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[{"cid": 111, "part": "p1"}])
        instance.get_danmakus = AsyncMock(return_value=[Danmaku("hello", dm_time=1.5)])

        result = await spec.callable("BV1", None)

    row = result["danmakus"][0]["result"][0]
    assert row["text"] == "hello"
    assert row["dm_time"] == 1.5


async def test_upower_qa_detail_item_uses_parent_uid():
    spec = get_endpoint("upower_qa_detail")
    assert spec is not None

    with patch("bili_unit.fetching._bilibili_adapter.user.User") as MockUser:
        instance = MockUser.return_value
        instance.get_upower_qa_detail = AsyncMock(return_value={"qa_id": 42, "content": "full"})

        result = await spec.callable("42", None, _uid=123)

    MockUser.assert_called_once()
    assert result["content"] == "full"
    instance.get_upower_qa_detail.assert_awaited_once_with(42)


# ---------------------------------------------------------------------------
# Runner fan-out integration: a video-level item endpoint actually persists
# per-item raw payloads via the SQLite store.
# ---------------------------------------------------------------------------

async def test_runner_can_fanout_new_video_endpoint(tmp_path: Path):
    settings = _settings(tmp_path)
    rl = _rate_limit()
    uid = 2

    async def fake_item(bvid, cred, **kw):
        return {"subtitle": [{"bvid": bvid}]}

    async def fake_fetch_endpoint(uid_arg, spec, credential, request_params, **kw):
        assert spec.name == "videos"
        return FetchPageResult(
            uid=uid_arg,
            endpoint="videos",
            raw_payload={
                "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                "page": {"count": 2},
            },
            is_last_page=True,
        )

    spec = get_endpoint("video_subtitle")
    assert spec is not None

    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_item)):
            runner = Runner(
                store, rl, settings,
                fetch_fn=AsyncMock(side_effect=fake_fetch_endpoint),
            )
            result = await runner.run_or_resume(
                uid, endpoints=["video_subtitle"], mode="incremental",
            )

        assert result.endpoints["video_subtitle"] == EndpointStatus.SUCCESS

        # raw_payload table now has BV1 and BV2 entries for video_subtitle.
        items = await store.list_completed_items("video_subtitle")
        assert items == ["BV1", "BV2"]
        bv1 = await store.get_raw_payload("video_subtitle", "BV1")
        bv2 = await store.get_raw_payload("video_subtitle", "BV2")
        assert bv1 == {"subtitle": [{"bvid": "BV1"}]}
        assert bv2 == {"subtitle": [{"bvid": "BV2"}]}
    finally:
        await ctx.close()
