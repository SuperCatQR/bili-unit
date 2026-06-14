# tests for the expanded fetching endpoint surface.

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import EndpointStatus
from bili_unit.fetching._bilibili_adapter import (
    FetchPageResult,
    fetch_endpoint,
)
from bili_unit.fetching._endpoint_catalog import ENDPOINTS, get_endpoint
from bili_unit.fetching.keys import _item_fetch_key
from bili_unit.fetching.query import Query
from bili_unit.fetching.runner import Runner

USER_EXTENDED_ENDPOINTS = {
    "access_id",
    "channels",
    "media_list",
    "dynamics_legacy",
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


@pytest.mark.asyncio
async def test_fetch_endpoint_legacy_offset_pagination():
    spec = get_endpoint("dynamics_legacy")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        offset = kw.get("offset", 0)
        if offset == 0:
            return {
                "cards": [{"desc": {"dynamic_id": "1"}}],
                "has_more": 1,
                "next_offset": 99,
            }
        return {
            "cards": [{"desc": {"dynamic_id": "2"}}],
            "has_more": 0,
            "next_offset": 0,
        }

    with patch.object(spec, "callable", fake_call):
        r1 = await fetch_endpoint(1, spec, None, {"offset": 0, "need_top": False})
        assert not r1.is_last_page
        assert r1.next_request == {"offset": 99, "need_top": False}

        r2 = await fetch_endpoint(1, spec, None, r1.next_request)
        assert r2.is_last_page


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_generic_query_item_access(stores):
    ds, es = stores
    uid = 1
    await ds.put(_item_fetch_key(uid, "video_subtitle", "BV1"), {
        "uid": uid,
        "endpoint": "video_subtitle",
        "item_id": "BV1",
        "status": "SUCCESS",
        "raw_payload": {"subtitle": []},
        "fetched_at": 100,
    })

    qry = Query(ds, es)
    listed = await qry.list_items(uid, "video_subtitle")
    assert listed == [("BV1", EndpointStatus.SUCCESS)]

    dto = await qry.get_item(uid, "video_subtitle", "BV1")
    assert dto is not None
    assert dto.available
    assert dto.raw_payload == {"subtitle": []}

    payloads = await qry.list_fanout_payloads(uid, "video_subtitle")
    assert payloads == {"BV1": {"subtitle": []}}


@pytest.mark.asyncio
async def test_runner_can_fanout_new_video_endpoint(stores, rl_ctl):
    ds, es = stores
    uid = 2

    async def fake_item(bvid, cred, **kw):
        return {"subtitle": [{"bvid": bvid}]}

    async def fake_fetch_endpoint(uid, spec, credential, request_params, **kw):
        assert spec.name == "videos"
        return FetchPageResult(
            uid=uid,
            endpoint="videos",
            raw_payload={
                "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                "page": {"count": 2},
            },
            is_last_page=True,
        )

    spec = get_endpoint("video_subtitle")
    assert spec is not None

    with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_item)):
        result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch_endpoint)).run_or_resume(
            uid, endpoints=["video_subtitle"], mode="incremental",
        )

    assert result.endpoints["video_subtitle"] == EndpointStatus.SUCCESS
    assert await ds.get(_item_fetch_key(uid, "video_subtitle", "BV1")) is not None
    assert await ds.get(_item_fetch_key(uid, "video_subtitle", "BV2")) is not None
