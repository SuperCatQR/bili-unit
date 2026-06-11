# tests for video_detail (item-level fan-out endpoint)
# Run: uv run pytest bili_unit/tests/test_video_detail.py -v

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import (
    EndpointStatus,
    Http412Error,
    RequestError,
    ResourceUnavailableError,
    TaskStatus,
)
from bili_unit.fetching.client import (
    ENDPOINTS,
    _extract_bvids_from_videos,
    fetch_video_detail_item,
    get_endpoint,
)
from bili_unit.fetching.keys import (
    _fetch_key,
    _item_fetch_key,
    _progress_key,
)
from bili_unit.fetching.runner import Runner
from bili_unit.fetching.task import EndpointEntry, TaskValue

# ======================================================================
# Client — _extract_bvids_from_videos
# ======================================================================

def test_extract_bvids_from_videos_basic():
    payload = {
        "pages": [
            {"list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]}, "page": {"count": 2}},
        ],
    }
    assert _extract_bvids_from_videos(payload) == ["BV1", "BV2"]


def test_extract_bvids_from_videos_multi_page():
    payload = {
        "pages": [
            {"list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]}},
            {"list": {"vlist": [{"bvid": "BV3"}]}},
        ],
    }
    assert _extract_bvids_from_videos(payload) == ["BV1", "BV2", "BV3"]


def test_extract_bvids_from_videos_empty():
    assert _extract_bvids_from_videos({"pages": []}) == []
    assert _extract_bvids_from_videos({}) == []


def test_extract_bvids_from_videos_skips_missing_bvid():
    payload = {
        "pages": [
            {"list": {"vlist": [{"bvid": "BV1"}, {"aid": 123}]}},
        ],
    }
    assert _extract_bvids_from_videos(payload) == ["BV1"]


# ======================================================================
# Client — video_detail endpoint registration
# ======================================================================

def test_video_detail_endpoint_registered():
    spec = get_endpoint("video_detail")
    assert spec is not None
    assert spec.name == "video_detail"
    assert spec.kind == "item"
    assert spec.source_endpoint == "videos"
    assert spec.rate_limit_key == "video_detail"
    assert spec.pagination_strategy == "none"
    assert spec.extract_items is not None


def test_existing_endpoints_are_uid_kind():
    _item_endpoints = {
        "video_detail",
        "article_detail",
        "opus_detail",
        "article_list_detail",
        "channel_videos_season",
        "channel_videos_series",
    }
    for ep in ENDPOINTS:
        if ep.name not in _item_endpoints:
            assert ep.kind == "uid", f"{ep.name} should have kind='uid'"


# ======================================================================
# Client — fetch_video_detail_item
# ======================================================================

@pytest.mark.asyncio
async def test_fetch_video_detail_item_success():
    fake_info = {"bvid": "BV1", "title": "test"}
    fake_tags = [{"tag_id": 1, "tag_name": "python"}]

    with patch(
        "bili_unit.fetching.client.Video",
    ) as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(return_value=fake_info)
        instance.get_tags = AsyncMock(return_value=fake_tags)

        result = await fetch_video_detail_item("BV1", None)

    assert result == {"info": fake_info, "tags": fake_tags}


@pytest.mark.asyncio
async def test_fetch_video_detail_item_info_412():
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(side_effect=ResponseCodeException(412, "too fast", {}))

        with pytest.raises(Http412Error):
            await fetch_video_detail_item("BV1", None)


@pytest.mark.asyncio
async def test_fetch_video_detail_item_tags_error():
    with patch("bili_unit.fetching.client.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(return_value={"bvid": "BV1"})
        instance.get_tags = AsyncMock(side_effect=RequestError("tags failed"))

        with pytest.raises(RequestError):
            await fetch_video_detail_item("BV1", None)


@pytest.mark.asyncio
async def test_fetch_video_detail_item_permanent_business_code_maps_to_unavailable():
    """Permanent business code from get_info is surfaced as ResourceUnavailableError."""
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(53013, "用户隐私设置未公开", {}),
        )

        with pytest.raises(ResourceUnavailableError):
            await fetch_video_detail_item("BV1", None)


# ======================================================================
# Task — item_progress serialization
# ======================================================================

def test_endpoint_entry_item_progress_roundtrip():
    tv = TaskValue(uid=1, status=TaskStatus.RUNNING)
    tv.endpoints["video_detail"] = EndpointEntry(
        status=EndpointStatus.RUNNING,
        item_progress={"total": 10, "completed": 5, "failed": 1},
    )

    d = tv.to_dict()
    assert d["endpoints"]["video_detail"]["item_progress"]["total"] == 10

    restored = TaskValue.from_dict(d)
    ip = restored.endpoints["video_detail"].item_progress
    assert ip is not None
    assert ip["completed"] == 5
    assert ip["failed"] == 1


def test_endpoint_entry_without_item_progress():
    tv = TaskValue(uid=1, status=TaskStatus.SUCCESS)
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.SUCCESS)

    d = tv.to_dict()
    assert "item_progress" not in d["endpoints"]["user_info"]

    restored = TaskValue.from_dict(d)
    assert restored.endpoints["user_info"].item_progress is None


# ======================================================================
# Rate limit — video_detail independent QPS
# ======================================================================

def test_rate_limit_video_detail_independent_qps():
    from bili_unit.fetching.rate_limit import RateLimitController

    rl = RateLimitController(
        global_qps=1.0, endpoint_qps=0.5, video_detail_qps=0.1,
    )
    # Verify internal state stores video_detail_qps
    assert rl._video_detail_qps == 0.1

    # to_state for video_detail should use video_detail_qps
    state = rl.to_state(endpoint="video_detail")
    assert state["qps"] == 0.1

    # to_state for other endpoints should use endpoint_qps
    state = rl.to_state(endpoint="videos")
    assert state["qps"] == 0.5


# ======================================================================
# Runner — Phase 2: item-level fan-out
# ======================================================================

def _seed_videos_data(ds, uid, bvids):
    """Seed videos endpoint data with given bvids."""
    return ds.put(_fetch_key(uid, "videos"), {
        "uid": uid, "endpoint": "videos", "status": "SUCCESS",
        "raw_payload": {
            "pages": [
                {"list": {"vlist": [{"bvid": bv} for bv in bvids]}, "page": {"count": len(bvids)}}
            ]
        },
        "fetched_at": 0, "updated_at": 0,
    })


def _seed_task(ds, uid, endpoint_statuses, task_status=TaskStatus.SUCCESS):
    """Seed a task with given endpoint statuses."""
    tv = TaskValue(uid=uid, status=task_status)
    for ep, status in endpoint_statuses.items():
        tv.endpoints[ep] = EndpointEntry(status=status)
    return ds.put(f"uid:{uid}:task", tv.to_dict())


@pytest.mark.asyncio
async def test_video_detail_basic_fanout(stores, rl_ctl):
    """Run video_detail after videos SUCCESS → all items fetched."""
    ds, es = stores

    # Seed videos data
    await _seed_videos_data(ds, 200, ["BV1", "BV2", "BV3"])
    await _seed_task(ds, 200, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    fake_results = {}

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fake_results[bvid] = {"info": {"bvid": bvid, "title": f"Title {bvid}"}, "tags": []}
        return fake_results[bvid]

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            200, endpoints=["video_detail"], mode="incremental",
        )

    assert result.status == TaskStatus.SUCCESS
    assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS

    # Verify per-bvid storage
    for bvid in ["BV1", "BV2", "BV3"]:
        stored = await ds.get(_item_fetch_key(200, "video_detail", bvid))
        assert stored is not None
        assert stored["raw_payload"]["info"]["bvid"] == bvid

    # Verify endpoint-level fetch key written (query-layer Bug 1 fix)
    fetch_data = await ds.get(_fetch_key(200, "video_detail"))
    assert fetch_data is not None
    assert fetch_data["status"] == "SUCCESS"
    assert fetch_data["item_counts"]["total"] == 3
    assert fetch_data["item_counts"]["completed"] == 3
    assert fetch_data["item_counts"]["failed"] == 0


@pytest.mark.asyncio
async def test_video_detail_source_not_available(stores, rl_ctl):
    """video_detail fails permanently when videos data is missing."""
    ds, es = stores

    # Seed task with videos SUCCESS but NO actual fetch data
    await _seed_task(ds, 201, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    result = await Runner(ds, es, rl_ctl).run_or_resume(
        201, endpoints=["video_detail"], mode="incremental",
    )

    # videos has SUCCESS status but no fetch data → video_detail FAILED_PERMANENT
    assert result.endpoints.get("video_detail") == EndpointStatus.FAILED_PERMANENT

    # Verify endpoint-level fetch key written for FAILED_PERMANENT
    fetch_data = await ds.get(_fetch_key(201, "video_detail"))
    assert fetch_data is not None
    assert fetch_data["status"] == "FAILED_PERMANENT"


@pytest.mark.asyncio
async def test_video_detail_incremental_skip_stored(stores, rl_ctl):
    """Incremental mode skips already-stored bvids."""
    ds, es = stores

    await _seed_videos_data(ds, 202, ["BV1", "BV2", "BV3"])
    await _seed_task(ds, 202, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    # Pre-store BV1
    await ds.put(_item_fetch_key(202, "video_detail", "BV1"), {
        "uid": 202, "endpoint": "video_detail", "item_id": "BV1",
        "status": "SUCCESS", "raw_payload": {"info": {}, "tags": []},
        "fetched_at": 0, "updated_at": 0,
    })

    fetched = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched.append(bvid)
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            202, endpoints=["video_detail"], mode="incremental",
        )

    assert result.status == TaskStatus.SUCCESS
    # BV1 should be skipped, only BV2 and BV3 fetched
    assert "BV1" not in fetched
    assert set(fetched) == {"BV2", "BV3"}


@pytest.mark.asyncio
async def test_video_detail_full_mode_refetches_all(stores, rl_ctl):
    """Full mode re-fetches all bvids, ignoring stored data."""
    ds, es = stores

    await _seed_videos_data(ds, 203, ["BV1", "BV2"])
    await _seed_task(ds, 203, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    # Pre-store BV1 with old data
    await ds.put(_item_fetch_key(203, "video_detail", "BV1"), {
        "uid": 203, "endpoint": "video_detail", "item_id": "BV1",
        "status": "SUCCESS", "raw_payload": {"info": {"old": True}, "tags": []},
        "fetched_at": 0, "updated_at": 0,
    })

    fetched = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched.append(bvid)
        return {"info": {"bvid": bvid, "new": True}, "tags": []}

    from bili_unit.fetching.client import FetchPageResult

    async def fake_fetch_endpoint(uid, spec, credential, request_params, **kw):
        if spec.name == "videos":
            return FetchPageResult(
                uid=uid, endpoint="videos",
                raw_payload={
                    "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                    "page": {"count": 2},
                },
                is_last_page=True, next_request=None,
            )
        return FetchPageResult(uid=uid, endpoint=spec.name, raw_payload={}, is_last_page=True)

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(side_effect=fake_fetch_endpoint),
    ), patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            203, endpoints=["video_detail"], mode="full",
        )

    assert result.status == TaskStatus.SUCCESS
    assert set(fetched) == {"BV1", "BV2"}

    # BV1 should be overwritten
    stored = await ds.get(_item_fetch_key(203, "video_detail", "BV1"))
    assert stored["raw_payload"]["info"].get("new") is True


@pytest.mark.asyncio
async def test_video_detail_partial_item_status(stores, rl_ctl):
    """Some items fail → PARTIAL_ITEM status."""
    ds, es = stores

    await _seed_videos_data(ds, 204, ["BV1", "BV2", "BV3"])
    await _seed_task(ds, 204, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        if bvid == "BV2":
            raise RequestError("permanent fail")
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            204, endpoints=["video_detail"], mode="incremental",
        )

    assert result.endpoints.get("video_detail") == EndpointStatus.PARTIAL_ITEM
    # BV1 and BV3 should succeed, BV2 should fail
    assert (await ds.get(_item_fetch_key(204, "video_detail", "BV1"))) is not None
    assert (await ds.get(_item_fetch_key(204, "video_detail", "BV2"))) is None
    assert (await ds.get(_item_fetch_key(204, "video_detail", "BV3"))) is not None


@pytest.mark.asyncio
async def test_video_detail_empty_items(stores, rl_ctl):
    """videos has no bvids → video_detail SUCCESS immediately."""
    ds, es = stores

    await _seed_videos_data(ds, 205, [])
    await _seed_task(ds, 205, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    result = await Runner(ds, es, rl_ctl).run_or_resume(
        205, endpoints=["video_detail"], mode="incremental",
    )

    assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS


@pytest.mark.asyncio
async def test_video_detail_two_phase_with_videos(stores, rl_ctl):
    """Running both videos + video_detail → Phase 1 runs videos, Phase 2 runs video_detail."""
    ds, es = stores

    from bili_unit.fetching.client import FetchPageResult

    async def fake_fetch_endpoint(uid, spec, credential, request_params, **kw):
        if spec.name == "videos":
            return FetchPageResult(
                uid=uid, endpoint="videos",
                raw_payload={
                    "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                    "page": {"count": 2},
                },
                is_last_page=True, next_request=None,
            )
        return FetchPageResult(
            uid=uid, endpoint=spec.name,
            raw_payload={"ok": True}, is_last_page=True,
        )

    fetched_items = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched_items.append(bvid)
        return {"info": {"bvid": bvid}, "tags": []}

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(side_effect=fake_fetch_endpoint),
    ), patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_task(
            206, endpoints=["videos", "video_detail"], mode="incremental",
        )

    assert result.status == TaskStatus.SUCCESS
    assert set(fetched_items) == {"BV1", "BV2"}


# ======================================================================
# Query — get_video_detail / list_video_details
# ======================================================================

@pytest.mark.asyncio
async def test_query_get_video_detail(query, stores):
    ds, _ = stores
    uid = 300

    await ds.put(_item_fetch_key(uid, "video_detail", "BV1"), {
        "uid": uid, "endpoint": "video_detail", "item_id": "BV1",
        "status": "SUCCESS",
        "raw_payload": {"info": {"bvid": "BV1", "title": "Hello"}, "tags": [{"tag_name": "test"}]},
        "fetched_at": 1000, "updated_at": 1000,
    })

    dto = await query.get_video_detail(uid, "BV1")
    assert dto is not None
    assert dto.available
    assert dto.raw_payload["info"]["title"] == "Hello"

    # Non-existent
    dto2 = await query.get_video_detail(uid, "BV_NONEXIST")
    assert dto2 is None


@pytest.mark.asyncio
async def test_query_list_video_details(query, stores):
    ds, _ = stores
    uid = 301

    for bv in ["BV1", "BV2", "BV3"]:
        await ds.put(_item_fetch_key(uid, "video_detail", bv), {
            "uid": uid, "endpoint": "video_detail", "item_id": bv,
            "status": "SUCCESS", "raw_payload": {}, "fetched_at": 0, "updated_at": 0,
        })

    results = await query.list_video_details(uid)
    bvids = [bv for bv, _ in results]
    assert set(bvids) == {"BV1", "BV2", "BV3"}
    assert all(s == EndpointStatus.SUCCESS for _, s in results)


@pytest.mark.asyncio
async def test_query_list_video_details_empty(query, stores):
    results = await query.list_video_details(999)
    assert results == []


# ======================================================================
# Runner — _derive_status with PARTIAL_ITEM
# ======================================================================

def test_derive_status_partial_item_counts_as_success():
    tv = TaskValue(uid=1, status=TaskStatus.RUNNING)
    tv.endpoints["videos"] = EndpointEntry(status=EndpointStatus.SUCCESS)
    tv.endpoints["video_detail"] = EndpointEntry(status=EndpointStatus.PARTIAL_ITEM)

    result = Runner._derive_status(tv)
    assert result == TaskStatus.SUCCESS


def test_derive_status_all_success_or_partial_item():
    tv = TaskValue(uid=1, status=TaskStatus.RUNNING)
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.SUCCESS)
    tv.endpoints["video_detail"] = EndpointEntry(status=EndpointStatus.PARTIAL_ITEM)

    result = Runner._derive_status(tv)
    assert result == TaskStatus.SUCCESS


def test_derive_status_partial_item_with_failure():
    tv = TaskValue(uid=1, status=TaskStatus.RUNNING)
    tv.endpoints["videos"] = EndpointEntry(status=EndpointStatus.FAILED_PERMANENT)
    tv.endpoints["video_detail"] = EndpointEntry(status=EndpointStatus.PARTIAL_ITEM)

    result = Runner._derive_status(tv)
    assert result == TaskStatus.PARTIAL


# ======================================================================
# Runner — item-level progress tracking
# ======================================================================

@pytest.mark.asyncio
async def test_video_detail_progress_tracking(stores, rl_ctl):
    """Verify progress is updated after each item."""
    ds, es = stores

    await _seed_videos_data(ds, 210, ["BV1", "BV2"])
    await _seed_task(ds, 210, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        await Runner(ds, es, rl_ctl).run_or_resume(
            210, endpoints=["video_detail"], mode="incremental",
        )

    # Check progress
    progress = await ds.get(_progress_key(210, "video_detail"))
    assert progress is not None
    assert progress["done"] is True
    assert progress["completed_items"] == 2
    assert progress["total_items"] == 2


# ======================================================================
# Runner — concurrent item processing
# ======================================================================

@pytest.mark.asyncio
async def test_video_detail_items_processed_concurrently(stores, rl_ctl):
    """Verify items are processed concurrently (not purely sequentially)."""
    import asyncio

    ds, es = stores

    await _seed_videos_data(ds, 220, ["BV1", "BV2", "BV3"])
    await _seed_task(ds, 220, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    in_flight = 0
    max_concurrent = 0
    # All fetches wait on this gate; released once all 3 are in-flight.
    release = asyncio.Event()

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        if in_flight >= 3:
            release.set()
        await release.wait()
        in_flight -= 1
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            220, endpoints=["video_detail"], mode="incremental",
        )

    assert result.status == TaskStatus.SUCCESS
    # All 3 items were in-flight simultaneously → concurrency works.
    assert max_concurrent >= 2, f"Expected concurrent processing, max_concurrent={max_concurrent}"


@pytest.mark.asyncio
async def test_video_detail_concurrent_partial_failure(stores, rl_ctl):
    """Concurrent processing: some items fail, others succeed → PARTIAL_ITEM."""
    ds, es = stores

    await _seed_videos_data(ds, 221, ["BV1", "BV2", "BV3", "BV4"])
    await _seed_task(ds, 221, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        if bvid in ("BV2", "BV4"):
            raise RequestError("fail this one")
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            221, endpoints=["video_detail"], mode="incremental",
        )

    assert result.endpoints.get("video_detail") == EndpointStatus.PARTIAL_ITEM
    # BV1 and BV3 stored, BV2 and BV4 not stored
    assert (await ds.get(_item_fetch_key(221, "video_detail", "BV1"))) is not None
    assert (await ds.get(_item_fetch_key(221, "video_detail", "BV2"))) is None
    assert (await ds.get(_item_fetch_key(221, "video_detail", "BV3"))) is not None
    assert (await ds.get(_item_fetch_key(221, "video_detail", "BV4"))) is None


# ======================================================================
# Runner — refresh mode (soft incremental with freshness window)
# ======================================================================

@pytest.mark.asyncio
async def test_refresh_mode_skips_fresh_items(stores, rl_ctl):
    """Refresh mode skips items fetched within the freshness window."""
    import time as _time

    ds, es = stores

    await _seed_videos_data(ds, 230, ["BV1", "BV2"])
    await _seed_task(ds, 230, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    # BV1 fetched recently (within 7-day window)
    recent_ms = int(_time.time() * 1000)
    await ds.put(_item_fetch_key(230, "video_detail", "BV1"), {
        "uid": 230, "endpoint": "video_detail", "item_id": "BV1",
        "status": "SUCCESS", "raw_payload": {"info": {"old": True}, "tags": []},
        "fetched_at": recent_ms, "updated_at": recent_ms,
    })

    fetched = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched.append(bvid)
        return {"info": {"bvid": bvid, "new": True}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            230, endpoints=["video_detail"], mode="refresh",
        )

    assert result.status == TaskStatus.SUCCESS
    # BV1 is fresh → skipped. BV2 is missing → fetched.
    assert "BV1" not in fetched
    assert "BV2" in fetched


@pytest.mark.asyncio
async def test_refresh_mode_refetches_stale_items(stores, rl_ctl):
    """Refresh mode re-fetches items older than the freshness window."""
    import time as _time

    ds, es = stores

    await _seed_videos_data(ds, 231, ["BV1", "BV2"])
    await _seed_task(ds, 231, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    # BV1 fetched 10 days ago (outside 7-day window)
    stale_ms = int((_time.time() - 10 * 86400) * 1000)
    await ds.put(_item_fetch_key(231, "video_detail", "BV1"), {
        "uid": 231, "endpoint": "video_detail", "item_id": "BV1",
        "status": "SUCCESS", "raw_payload": {"info": {"old": True}, "tags": []},
        "fetched_at": stale_ms, "updated_at": stale_ms,
    })

    # BV2 fetched recently
    recent_ms = int(_time.time() * 1000)
    await ds.put(_item_fetch_key(231, "video_detail", "BV2"), {
        "uid": 231, "endpoint": "video_detail", "item_id": "BV2",
        "status": "SUCCESS", "raw_payload": {"info": {"ok": True}, "tags": []},
        "fetched_at": recent_ms, "updated_at": recent_ms,
    })

    fetched = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched.append(bvid)
        return {"info": {"bvid": bvid, "refreshed": True}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            231, endpoints=["video_detail"], mode="refresh",
        )

    assert result.status == TaskStatus.SUCCESS
    # BV1 is stale (10 days > 7 day window) → re-fetched
    assert "BV1" in fetched
    # BV2 is fresh → skipped
    assert "BV2" not in fetched
    # BV1 should be overwritten with new data
    stored = await ds.get(_item_fetch_key(231, "video_detail", "BV1"))
    assert stored["raw_payload"]["info"].get("refreshed") is True


@pytest.mark.asyncio
async def test_refresh_mode_behaves_like_incremental_for_new_items(stores, rl_ctl):
    """Refresh mode fetches new items just like incremental mode."""
    ds, es = stores

    await _seed_videos_data(ds, 232, ["BV1", "BV2", "BV3"])
    await _seed_task(ds, 232, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    fetched = []

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        fetched.append(bvid)
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            232, endpoints=["video_detail"], mode="refresh",
        )

    assert result.status == TaskStatus.SUCCESS
    assert set(fetched) == {"BV1", "BV2", "BV3"}


# ======================================================================
# Bug fix verification — endpoint-level fetch key for item endpoints
# ======================================================================

@pytest.mark.asyncio
async def test_query_endpoint_shows_video_detail_status_after_fanout(stores, rl_ctl):
    """After _run_item_endpoint completes, query.get_endpoint returns correct
    status for video_detail (Bug 1 fix: endpoint-level fetch key is written)."""
    ds, es = stores
    uid = 240

    await _seed_videos_data(ds, uid, ["BV1", "BV2"])
    await _seed_task(ds, uid, {"videos": EndpointStatus.SUCCESS, "video_detail": EndpointStatus.PENDING})

    async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
        return {"info": {"bvid": bvid}, "tags": []}

    with patch.object(
        get_endpoint("video_detail"), "callable",
        new=AsyncMock(side_effect=fake_fetch_item),
    ):
        result = await Runner(ds, es, rl_ctl).run_or_resume(
            uid, endpoints=["video_detail"], mode="incremental",
        )

    assert result.status == TaskStatus.SUCCESS

    # The fix: query.get_endpoint should now return SUCCESS, not PENDING
    from bili_unit.fetching.query import Query
    query = Query(ds, es)
    ep_dto = await query.get_endpoint(uid, "video_detail")
    assert ep_dto is not None
    assert ep_dto.status == EndpointStatus.SUCCESS

    # Also verify via get_task (which calls get_endpoint internally)
    task_dto = await query.get_task(uid)
    assert task_dto is not None
    vd_dto = task_dto.endpoints.get("video_detail")
    assert vd_dto is not None
    assert vd_dto.status == EndpointStatus.SUCCESS
