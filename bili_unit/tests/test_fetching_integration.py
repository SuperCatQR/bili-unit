# integration tests — Command -> Runner -> SQLite stores end-to-end.
#
# Phase 6 rewrite: the legacy DataStore/ErrorStore + Query trio is gone. We
# drive Command (which opens its own UidContext) and verify state by reading
# from a fresh FetchingStore on the same tmp_path afterwards.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching._bilibili_adapter import FetchPageResult
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.command import Command
from bili_unit.fetching.rate_limit import RateLimitController


def _settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(bili_db_dir=str(tmp_path))


def _rate_limit() -> RateLimitController:
    return RateLimitController(
        global_qps=1000.0,
        endpoint_qps=1000.0,
        video_detail_qps=1000.0,
        pause_seconds=0,
    )


def _fake_page(uid: int, payload: dict, *, endpoint: str = "user_info") -> FetchPageResult:
    return FetchPageResult(
        uid=uid,
        endpoint=endpoint,
        raw_payload=payload,
        is_last_page=True,
        next_request=None,
    )


def _fake_videos_pages(uid: int, total_pages: int):
    """Yield a sequence of FetchPageResult for the videos endpoint.

    Mirrors the legacy conftest helper of the same name. First page has 30
    items, last page has 1 item, everything in between is full.
    """
    total_items = (total_pages - 1) * 30 + 1
    for pn in range(1, total_pages + 1):
        is_last = pn == total_pages
        n_items = 1 if is_last else 30
        yield FetchPageResult(
            uid=uid,
            endpoint="videos",
            raw_payload={
                "list": {
                    "vlist": [{"bvid": f"BV{pn:03d}{i:02d}"} for i in range(n_items)],
                },
                "page": {"count": total_items},
            },
            is_last_page=is_last,
            next_request=None if is_last else {"pn": pn + 1, "ps": 30},
        )


# ======================================================================
# full loop — single endpoint
# ======================================================================


async def test_integration_single_endpoint_success(tmp_path: Path):
    user_info_data = {"code": 0, "data": {"mid": 123, "name": "test"}}
    cmd = Command(
        _settings(tmp_path),
        _rate_limit(),
        fetch_fn=AsyncMock(return_value=_fake_page(123, user_info_data)),
    )

    result = await cmd.fetch_uid(123, endpoints=["user_info"])
    assert result.status == TaskStatus.SUCCESS

    # Verify persisted state via a fresh store read.
    ctx = UidContext(uid=123, root=tmp_path)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        assert await store.get_task_status() == TaskStatus.SUCCESS.value
        assert await store.get_endpoint_status("user_info") == EndpointStatus.SUCCESS.value
        payload = await store.get_raw_payload("user_info")
        assert payload == user_info_data
    finally:
        await ctx.close()


# ======================================================================
# full loop — multi-endpoint, multi-page videos
# ======================================================================


async def test_integration_multi_endpoint(tmp_path: Path):
    """user_info succeeds; videos succeeds with 3 pages."""
    pages = list(_fake_videos_pages(999, total_pages=3))

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"name": "a"})
        if spec.name == "videos":
            pn = request_params.get("pn", 1)
            if pn <= len(pages):
                return pages[pn - 1]
            return FetchPageResult(
                uid=uid,
                endpoint="videos",
                raw_payload={"list": {"vlist": []}, "page": {"count": 0}},
                is_last_page=True,
            )
        raise RuntimeError(f"unexpected {spec.name}")

    cmd = Command(
        _settings(tmp_path),
        _rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch),
    )
    result = await cmd.fetch_uid(999, endpoints=["user_info", "videos"])
    assert result.status == TaskStatus.SUCCESS

    ctx = UidContext(uid=999, root=tmp_path)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        assert await store.get_task_status() == TaskStatus.SUCCESS.value
        assert await store.get_endpoint_status("user_info") == EndpointStatus.SUCCESS.value
        assert await store.get_endpoint_status("videos") == EndpointStatus.SUCCESS.value

        videos_payload = await store.get_raw_payload("videos")
        assert videos_payload is not None
        assert "pages" in videos_payload
        assert len(videos_payload["pages"]) == 3
        # First page: 30 items, last page: 1 item (per _fake_videos_pages).
        assert len(videos_payload["pages"][0]["list"]["vlist"]) == 30
        assert len(videos_payload["pages"][2]["list"]["vlist"]) == 1
    finally:
        await ctx.close()


# ======================================================================
# resume — a partially-failed task completes on a second call
# ======================================================================


async def test_integration_resume_after_partial(tmp_path: Path):
    """First run partially fails on videos; second run resumes it to SUCCESS."""
    settings = _settings(tmp_path)

    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"name": "a"})
        # videos: simulate a permanent endpoint hiccup
        from bili_unit.fetching import Http412Error

        raise Http412Error("412")

    cmd1 = Command(
        settings,
        _rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch_1),
    )
    r1 = await cmd1.fetch_uid(444, endpoints=["user_info", "videos"])
    assert r1.status in (TaskStatus.PARTIAL, TaskStatus.FAILED_EXHAUSTED)

    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"name": "a"})
        if spec.name == "videos":
            return FetchPageResult(
                uid=uid,
                endpoint="videos",
                raw_payload={"list": {"vlist": []}, "page": {"count": 0}},
                is_last_page=True,
            )
        raise RuntimeError(spec.name)

    cmd2 = Command(
        settings,
        _rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch_2),
    )
    r2 = await cmd2.fetch_uid(444, endpoints=["user_info", "videos"])
    assert r2.status == TaskStatus.SUCCESS

    ctx = UidContext(uid=444, root=tmp_path)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        assert await store.get_task_status() == TaskStatus.SUCCESS.value
        assert await store.get_endpoint_status("videos") == EndpointStatus.SUCCESS.value
    finally:
        await ctx.close()
