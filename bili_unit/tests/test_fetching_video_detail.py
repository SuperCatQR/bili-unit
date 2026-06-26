# tests for video_detail (item-level fan-out endpoint).
# Phase 6 rewrite: Runner now writes via FetchingStore (SQLite); tests assert
# against the SQL tables instead of the legacy KV store.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import (
    EndpointStatus,
    Http412Error,
    RequestError,
    ResourceUnavailableError,
    TaskStatus,
)
from bili_unit.fetching._bilibili_adapter import (
    FetchPageResult,
    _extract_bvids_from_videos,
    fetch_video_detail_item,
)
from bili_unit.fetching._endpoint_catalog import ENDPOINTS, get_endpoint
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.runner import Runner

# REPLACE_ME_HELPERS


def _settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(bili_db_dir=str(tmp_path))


def _fast_rate_limit() -> RateLimitController:
    return RateLimitController(
        global_qps=1000.0,
        endpoint_qps=1000.0,
        video_detail_qps=1000.0,
        pause_seconds=0,
    )


async def _open_store(tmp_path: Path, uid: int) -> tuple[UidContext, FetchingStore]:
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    return ctx, FetchingStore(ctx)


async def _seed_videos_payload(store: FetchingStore, bvids: list[str]) -> None:
    """Seed the videos endpoint with a single-page payload and SUCCESS state.

    Marks the parent task as SUCCESS so that incremental-mode reruns enter the
    "scan" branch (which only resets endpoints in the explicit run list)
    rather than the resume branch (which would reset videos itself).
    """
    await store.init_task(["videos", "video_detail"])
    await store.save_raw_payload(
        "videos",
        "",
        {
            "pages": [
                {
                    "list": {"vlist": [{"bvid": bv} for bv in bvids]},
                    "page": {"count": len(bvids)},
                },
            ],
        },
    )
    await store.update_endpoint_state(
        "videos",
        status=EndpointStatus.SUCCESS.value,
    )
    await store.update_task_status(TaskStatus.SUCCESS.value)


def _runner(store: FetchingStore, settings: BiliSettings, *, fetch_fn=None) -> Runner:
    return Runner(store, _fast_rate_limit(), settings, fetch_fn=fetch_fn)


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
# Client — endpoint registration
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
    for ep in ENDPOINTS:
        if ep.source_endpoint is None:
            assert ep.kind == "uid", f"{ep.name} should have kind='uid'"
        else:
            assert ep.kind == "item", f"{ep.name} should have kind='item'"


# ======================================================================
# Client — fetch_video_detail_item
# ======================================================================


async def test_fetch_video_detail_item_success():
    fake_info = {"bvid": "BV1", "title": "test"}
    fake_tags = [{"tag_id": 1, "tag_name": "python"}]

    with patch(
        "bili_unit.fetching._bilibili_adapter.Video",
    ) as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(return_value=fake_info)
        instance.get_tags = AsyncMock(return_value=fake_tags)

        result = await fetch_video_detail_item("BV1", None)

    assert result == {"info": fake_info, "tags": fake_tags}


async def test_fetch_video_detail_item_info_412():
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(side_effect=ResponseCodeException(412, "too fast", {}))

        with pytest.raises(Http412Error):
            await fetch_video_detail_item("BV1", None)


async def test_fetch_video_detail_item_tags_error():
    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(return_value={"bvid": "BV1"})
        instance.get_tags = AsyncMock(side_effect=RequestError("tags failed"))

        with pytest.raises(RequestError):
            await fetch_video_detail_item("BV1", None)


async def test_fetch_video_detail_item_permanent_business_code_maps_to_unavailable():
    """Permanent business code from get_info is surfaced as ResourceUnavailableError."""
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(53013, "用户隐私设置未公开", {}),
        )

        with pytest.raises(ResourceUnavailableError):
            await fetch_video_detail_item("BV1", None)


# ======================================================================
# Rate limit — video_detail uses an independent QPS budget
# ======================================================================


def test_rate_limit_video_detail_independent_qps():
    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        video_detail_qps=0.1,
    )
    assert rl._video_detail_qps == 0.1
    assert rl._endpoint_qps == 0.5
    # Sanity: the original ceilings stick around for QPS recovery.
    assert rl._orig_video_detail_qps == 0.1
    assert rl._orig_endpoint_qps == 0.5


# ======================================================================
# Runner — Phase 2: item-level fan-out
# ======================================================================


async def test_video_detail_basic_fanout(tmp_path: Path):
    """Run video_detail after videos SUCCESS -> all items fetched."""
    ctx, store = await _open_store(tmp_path, 200)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            return {"info": {"bvid": bvid, "title": f"Title {bvid}"}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                200,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.status == TaskStatus.SUCCESS
        assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS

        # Per-bvid raw_payload rows.
        items = await store.list_completed_items("video_detail")
        assert items == ["BV1", "BV2", "BV3"]
        for bv in ["BV1", "BV2", "BV3"]:
            payload = await store.get_raw_payload("video_detail", bv)
            assert payload is not None
            assert payload["info"]["bvid"] == bv

        # endpoint state row reflects item progress.
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["status"] == EndpointStatus.SUCCESS.value
        assert state["item_progress"]["total"] == 3
        assert state["item_progress"]["completed"] == 3
        assert state["item_progress"]["failed"] == 0
    finally:
        await ctx.close()


async def test_video_detail_source_not_available(tmp_path: Path):
    """video_detail fails permanently when videos data is missing."""
    ctx, store = await _open_store(tmp_path, 201)
    try:
        # Mark videos SUCCESS but never write the actual payload.
        await store.init_task(["videos", "video_detail"])
        await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
        await store.update_task_status(TaskStatus.SUCCESS.value)

        result = await _runner(store, _settings(tmp_path)).run_or_resume(
            201,
            endpoints=["video_detail"],
            mode="incremental",
        )

        assert result.endpoints.get("video_detail") == EndpointStatus.FAILED_PERMANENT
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["status"] == EndpointStatus.FAILED_PERMANENT.value
    finally:
        await ctx.close()


async def test_video_detail_incremental_skip_stored(tmp_path: Path):
    """Incremental mode skips already-stored bvids."""
    ctx, store = await _open_store(tmp_path, 202)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])
        # Pre-store BV1.
        await store.save_raw_payload(
            "video_detail",
            "BV1",
            {"info": {}, "tags": []},
        )

        fetched: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched.append(bvid)
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                202,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.status == TaskStatus.SUCCESS
        assert "BV1" not in fetched
        assert set(fetched) == {"BV2", "BV3"}
    finally:
        await ctx.close()


async def test_video_detail_full_mode_refetches_all(tmp_path: Path):
    """Full mode re-fetches all bvids, ignoring stored data."""
    ctx, store = await _open_store(tmp_path, 203)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])
        # Pre-store BV1 with old data.
        await store.save_raw_payload(
            "video_detail",
            "BV1",
            {"info": {"bvid": "BV1", "old": True}, "tags": []},
        )

        fetched: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched.append(bvid)
            return {"info": {"bvid": bvid, "new": True}, "tags": []}

        async def fake_fetch_endpoint(uid, spec, credential, request_params, **kw):
            if spec.name == "videos":
                return FetchPageResult(
                    uid=uid,
                    endpoint="videos",
                    raw_payload={
                        "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                        "page": {"count": 2},
                    },
                    is_last_page=True,
                    next_request=None,
                )
            return FetchPageResult(
                uid=uid,
                endpoint=spec.name,
                raw_payload={},
                is_last_page=True,
            )

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(
                store,
                _settings(tmp_path),
                fetch_fn=AsyncMock(side_effect=fake_fetch_endpoint),
            ).run_or_resume(
                203,
                endpoints=["video_detail"],
                mode="full",
            )

        assert result.status == TaskStatus.SUCCESS
        assert set(fetched) == {"BV1", "BV2"}

        # BV1 was overwritten with the "new" payload.
        bv1 = await store.get_raw_payload("video_detail", "BV1")
        assert bv1 is not None
        assert bv1["info"].get("new") is True
    finally:
        await ctx.close()


async def test_video_detail_partial_item_status(tmp_path: Path):
    """Some items fail -> PARTIAL_ITEM status."""
    ctx, store = await _open_store(tmp_path, 204)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            if bvid == "BV2":
                raise RequestError("permanent fail")
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                204,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.endpoints.get("video_detail") == EndpointStatus.PARTIAL_ITEM
        # Successes are stored; failures are not.
        assert (await store.get_raw_payload("video_detail", "BV1")) is not None
        assert (await store.get_raw_payload("video_detail", "BV2")) is None
        assert (await store.get_raw_payload("video_detail", "BV3")) is not None
    finally:
        await ctx.close()


async def test_video_detail_empty_items(tmp_path: Path):
    """videos has no bvids -> video_detail SUCCESS immediately."""
    ctx, store = await _open_store(tmp_path, 205)
    try:
        await _seed_videos_payload(store, [])

        result = await _runner(store, _settings(tmp_path)).run_or_resume(
            205,
            endpoints=["video_detail"],
            mode="incremental",
        )

        assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["item_progress"] == {"total": 0, "completed": 0, "failed": 0}
    finally:
        await ctx.close()


async def test_video_detail_two_phase_with_videos(tmp_path: Path):
    """Running both videos + video_detail -> phase 1 videos, phase 2 video_detail."""
    ctx, store = await _open_store(tmp_path, 206)
    try:

        async def fake_fetch_endpoint(uid, spec, credential, request_params, **kw):
            if spec.name == "videos":
                return FetchPageResult(
                    uid=uid,
                    endpoint="videos",
                    raw_payload={
                        "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
                        "page": {"count": 2},
                    },
                    is_last_page=True,
                    next_request=None,
                )
            return FetchPageResult(
                uid=uid,
                endpoint=spec.name,
                raw_payload={"ok": True},
                is_last_page=True,
            )

        fetched_items: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched_items.append(bvid)
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(
                store,
                _settings(tmp_path),
                fetch_fn=AsyncMock(side_effect=fake_fetch_endpoint),
            ).run_task(
                206,
                endpoints=["videos", "video_detail"],
                mode="incremental",
            )

        assert result.status == TaskStatus.SUCCESS
        assert set(fetched_items) == {"BV1", "BV2"}
    finally:
        await ctx.close()


# ======================================================================
# Runner — _derive_status_from_statuses with PARTIAL_ITEM
# ======================================================================


def test_derive_status_partial_item_counts_as_success():
    statuses = [EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM]
    assert Runner._derive_status_from_statuses(statuses) == TaskStatus.SUCCESS


def test_derive_status_all_success_or_partial_item():
    statuses = [EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM]
    assert Runner._derive_status_from_statuses(statuses) == TaskStatus.SUCCESS


def test_derive_status_partial_item_with_failure():
    statuses = [
        EndpointStatus.FAILED_PERMANENT,
        EndpointStatus.PARTIAL_ITEM,
    ]
    assert Runner._derive_status_from_statuses(statuses) == TaskStatus.PARTIAL


# ======================================================================
# Runner — item-level progress tracking
# ======================================================================


async def test_video_detail_progress_tracking(tmp_path: Path):
    """Progress is updated after each item; final cursor=None and counters set."""
    ctx, store = await _open_store(tmp_path, 210)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            await _runner(store, _settings(tmp_path)).run_or_resume(
                210,
                endpoints=["video_detail"],
                mode="incremental",
            )

        # fetch_progress row reflects completion: cursor=None, fetched=2, total=2.
        progress = await store.get_progress("video_detail")
        assert progress is not None
        assert progress["cursor"] is None
        assert progress["fetched"] == 2
        assert progress["total"] == 2

        # endpoint state's item_progress mirrors that.
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["item_progress"]["total"] == 2
        assert state["item_progress"]["completed"] == 2
        assert state["item_progress"]["failed"] == 0
    finally:
        await ctx.close()


# ======================================================================
# Runner — concurrent item processing
# ======================================================================


async def test_video_detail_items_processed_concurrently(tmp_path: Path):
    """Verify items are processed concurrently (not purely sequentially)."""
    import asyncio

    ctx, store = await _open_store(tmp_path, 220)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])

        in_flight = 0
        max_concurrent = 0
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

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                220,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.status == TaskStatus.SUCCESS
        # Bili default is 3 concurrent items; at the very least we must have
        # gone above 1 (proving real concurrency).
        assert max_concurrent >= 2, f"Expected concurrent processing, max_concurrent={max_concurrent}"
    finally:
        await ctx.close()


async def test_video_detail_concurrent_partial_failure(tmp_path: Path):
    """Concurrent processing with some failures -> PARTIAL_ITEM."""
    ctx, store = await _open_store(tmp_path, 221)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3", "BV4"])

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            if bvid in ("BV2", "BV4"):
                raise RequestError("fail this one")
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                221,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.endpoints.get("video_detail") == EndpointStatus.PARTIAL_ITEM
        assert (await store.get_raw_payload("video_detail", "BV1")) is not None
        assert (await store.get_raw_payload("video_detail", "BV2")) is None
        assert (await store.get_raw_payload("video_detail", "BV3")) is not None
        assert (await store.get_raw_payload("video_detail", "BV4")) is None
    finally:
        await ctx.close()


# ======================================================================
# Runner — refresh mode (incremental + freshness window)
# ======================================================================


async def test_refresh_mode_skips_fresh_items(tmp_path: Path):
    """Refresh mode skips items fetched within the freshness window."""
    import time as _time

    ctx, store = await _open_store(tmp_path, 230)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        # BV1 fetched recently (within the 7-day default window).
        recent_ms = int(_time.time() * 1000)
        await store.save_raw_payload(
            "video_detail",
            "BV1",
            {"info": {"bvid": "BV1", "old": True}, "tags": []},
            fetched_at_ms=recent_ms,
        )

        fetched: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched.append(bvid)
            return {"info": {"bvid": bvid, "new": True}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                230,
                endpoints=["video_detail"],
                mode="refresh",
            )

        assert result.status == TaskStatus.SUCCESS
        assert "BV1" not in fetched
        assert "BV2" in fetched
    finally:
        await ctx.close()


async def test_refresh_mode_refetches_stale_items(tmp_path: Path):
    """Refresh mode re-fetches items older than the freshness window."""
    import time as _time

    ctx, store = await _open_store(tmp_path, 231)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        stale_ms = int((_time.time() - 10 * 86400) * 1000)
        await store.save_raw_payload(
            "video_detail",
            "BV1",
            {"info": {"bvid": "BV1", "old": True}, "tags": []},
            fetched_at_ms=stale_ms,
        )
        recent_ms = int(_time.time() * 1000)
        await store.save_raw_payload(
            "video_detail",
            "BV2",
            {"info": {"bvid": "BV2", "ok": True}, "tags": []},
            fetched_at_ms=recent_ms,
        )

        fetched: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched.append(bvid)
            return {"info": {"bvid": bvid, "refreshed": True}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                231,
                endpoints=["video_detail"],
                mode="refresh",
            )

        assert result.status == TaskStatus.SUCCESS
        assert "BV1" in fetched  # stale -> re-fetched
        assert "BV2" not in fetched  # fresh -> skipped

        bv1 = await store.get_raw_payload("video_detail", "BV1")
        assert bv1 is not None
        assert bv1["info"].get("refreshed") is True
    finally:
        await ctx.close()


async def test_refresh_mode_behaves_like_incremental_for_new_items(tmp_path: Path):
    """Refresh fetches new items just like incremental."""
    ctx, store = await _open_store(tmp_path, 232)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])

        fetched: list[str] = []

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            fetched.append(bvid)
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                232,
                endpoints=["video_detail"],
                mode="refresh",
            )

        assert result.status == TaskStatus.SUCCESS
        assert set(fetched) == {"BV1", "BV2", "BV3"}
    finally:
        await ctx.close()


# ======================================================================
# Bug fix verification — endpoint state row reflects fanout outcome
# ======================================================================


async def test_endpoint_state_after_fanout_reflects_success(tmp_path: Path):
    """After _run_item_endpoint completes, get_endpoint_status returns
    SUCCESS — earlier query-layer bug used to misreport this as PENDING.
    """
    ctx, store = await _open_store(tmp_path, 240)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        async def fake_fetch_item(bvid, cred, timeout=30.0, **_kw):
            return {"info": {"bvid": bvid}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        with patch.object(spec, "callable", new=AsyncMock(side_effect=fake_fetch_item)):
            result = await _runner(store, _settings(tmp_path)).run_or_resume(
                240,
                endpoints=["video_detail"],
                mode="incremental",
            )

        assert result.status == TaskStatus.SUCCESS
        assert await store.get_endpoint_status("video_detail") == EndpointStatus.SUCCESS.value
    finally:
        await ctx.close()
