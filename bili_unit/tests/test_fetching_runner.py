# tests for bili_unit/fetching/runner
# Run: uv run pytest bili_unit/tests/test_runner.py -v

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import (
    AuthError,
    EndpointStatus,
    Http412Error,
    ResourceUnavailableError,
    TaskStatus,
)
from bili_unit.fetching._bilibili_adapter import FetchPageResult
from bili_unit.fetching._endpoint_catalog import get_endpoint
from bili_unit.fetching.keys import _fetch_key
from bili_unit.fetching.runner import Runner, _extract_item_ids
from bili_unit.fetching.task import EndpointEntry, TaskValue

from .conftest import _fake_page

# ======================================================================
# runner — single endpoint success
# ======================================================================

@pytest.mark.asyncio
async def test_runner_single_success(stores, rl_ctl):
    ds, es = stores
    runner = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(return_value=_fake_page(1, {"ok": True})))
    result = await runner.run_task(1, endpoints=["user_info"])
    assert result.status == TaskStatus.SUCCESS


# ======================================================================
# runner — partial failure (one success, one permanent fail)
# ======================================================================

@pytest.mark.asyncio
async def test_runner_partial(stores, rl_ctl):
    ds, es = stores

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True})
        if spec.name == "videos":
            raise Http412Error("412 videos")

    runner = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        # override retry delays to be instant for test
        result = await runner.run_task(2, endpoints=["user_info", "videos"])

    # videos retried 3 times and exhausted → PARTIAL (user_info SUCCESS)
    assert result.status == TaskStatus.PARTIAL
    assert result.endpoints.get("user_info") == EndpointStatus.SUCCESS
    assert result.endpoints.get("videos") in (
        EndpointStatus.FAILED_EXHAUSTED,
        EndpointStatus.FAILED_RETRYABLE,
    )


# ======================================================================
# runner — auth error → FAILED_PERMANENT (when credential_required)
# ======================================================================

@pytest.mark.asyncio
async def test_runner_auth_permanent_fail(stores, rl_ctl):
    ds, es = stores
    runner = Runner(ds, es, rl_ctl)

    # Mark user_info as requiring credential for this test
    spec = get_endpoint("user_info")
    with patch.object(spec, "credential_required", True), patch(
        "bili_unit.fetching.runner.get_credential",
        side_effect=AuthError("no sessdata"),
    ):
        result = await runner.run_task(3, endpoints=["user_info"])
    assert result.status == TaskStatus.FAILED_PERMANENT


# ======================================================================
# runner — auth is mandatory regardless of credential_required
# ======================================================================

@pytest.mark.asyncio
async def test_runner_auth_failure_is_permanent(stores, rl_ctl):
    ds, es = stores
    runner = Runner(ds, es, rl_ctl)

    # Auth is mandatory regardless of endpoint's credential_required flag.
    # Missing SESSDATA must cause FAILED_PERMANENT immediately.
    with patch(
        "bili_unit.fetching.runner.get_credential",
        side_effect=AuthError("no sessdata"),
    ):
        result = await runner.run_task(3, endpoints=["user_info"])
    assert result.status == TaskStatus.FAILED_PERMANENT


# ======================================================================
# runner — 412 retry → recover
# ======================================================================

@pytest.mark.asyncio
async def test_runner_412_retry_eventually_succeeds(stores, rl_ctl):
    ds, es = stores
    attempts = [0]

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        attempts[0] += 1
        if attempts[0] <= 2:
            raise Http412Error("too fast")
        return _fake_page(uid, {"ok": True})

    runner = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        result = await runner.run_task(4, endpoints=["user_info"])

    assert result.status == TaskStatus.SUCCESS
    assert attempts[0] == 3  # 2 fails + 1 success


# ======================================================================
# runner — progress resumption (videos multi-page with interrupt)
# ======================================================================

@pytest.mark.asyncio
async def test_runner_progress_resumption(stores, rl_ctl):
    ds, es = stores

    # First run: succeed page 1, fail page 2
    call_log = []

    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        pn = request_params.get("pn", 1)
        call_log.append(("run1", pn))
        if pn == 1:
            return FetchPageResult(
                uid=uid, endpoint="videos",
                raw_payload={"list": {"vlist": [{"aid": i} for i in range(30)]}, "page": {"count": 65}},
                is_last_page=False, next_request={"pn": 2, "ps": 30},
            )
        raise Http412Error("412 page 2")

    runner1 = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch_1))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        result = await runner1.run_task(5, endpoints=["videos"])

    assert result.status in (TaskStatus.FAILED_EXHAUSTED, TaskStatus.FAILED_RETRYABLE)

    # Verify progress was saved
    from bili_unit.fetching.keys import _progress_key
    prog = await ds.get(_progress_key(5, "videos"))
    assert prog is not None
    assert prog.get("last_completed_request", {}).get("pn") == 1
    assert prog.get("next_request", {}).get("pn") == 2

    # Second run (resume): should start from page 2
    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        pn = request_params.get("pn", 1)
        call_log.append(("run2", pn))
        return FetchPageResult(
            uid=uid, endpoint="videos",
            raw_payload={"list": {"vlist": [{"aid": 999}]}, "page": {"count": 65}},
            is_last_page=True, next_request=None,
        )

    runner2 = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch_2))
    await runner2.run_task(5, endpoints=["videos"])

    # Should have started from page 2
    assert ("run2", 2) in call_log


# ======================================================================
# _extract_item_ids — pure function tests
# ======================================================================

def test_extract_item_ids_videos_shape():
    """videos-style: list.vlist[*].bvid"""
    payload = {
        "list": {
            "vlist": [
                {"bvid": "BV111", "title": "a"},
                {"bvid": "BV222", "title": "b"},
            ]
        },
        "page": {"count": 2},
    }
    ids = _extract_item_ids(payload, "list.vlist[*].bvid")
    assert ids == ["BV111", "BV222"]


def test_extract_item_ids_dynamics_shape():
    """dynamics-style: items[*].id_str"""
    payload = {
        "items": [
            {"id_str": "111", "type": "DYNAMIC_TYPE_AV"},
            {"id_str": "222", "type": "DYNAMIC_TYPE_DRAW"},
        ],
        "has_more": 0,
    }
    ids = _extract_item_ids(payload, "items[*].id_str")
    assert ids == ["111", "222"]


def test_extract_item_ids_none_path():
    assert _extract_item_ids({"any": "data"}, None) == []


def test_extract_item_ids_missing_key():
    payload = {"list": {"other": []}}
    ids = _extract_item_ids(payload, "list.vlist[*].bvid")
    assert ids == []


def test_extract_item_ids_wrong_type_at_expand():
    """[*] position is not a list → empty result."""
    payload = {"list": {"vlist": "not a list"}}
    ids = _extract_item_ids(payload, "list.vlist[*].bvid")
    assert ids == []


def test_extract_item_ids_empty_list():
    payload = {"items": []}
    ids = _extract_item_ids(payload, "items[*].id_str")
    assert ids == []


def test_extract_item_ids_no_expand():
    """Path without [*] — returns single value as list."""
    payload = {"meta": {"id": 42}}
    ids = _extract_item_ids(payload, "meta.id")
    assert ids == ["42"]


def test_extract_item_ids_expand_is_last_segment():
    """[*] is the last segment — returns stringified elements."""
    payload = {"tags": ["a", "b", "c"]}
    ids = _extract_item_ids(payload, "tags[*]")
    assert ids == ["a", "b", "c"]


# ======================================================================
# Helpers for incremental / full mode tests
# ======================================================================

def _seed_video_store(ds, uid, bvids, ep="videos"):
    """Seed the data store with a videos-style payload."""
    return ds.put(_fetch_key(uid, ep), {
        "uid": uid,
        "endpoint": ep,
        "status": "SUCCESS",
        "raw_payload": {
            "pages": [
                {"list": {"vlist": [{"bvid": bv} for bv in bvids]}, "page": {"count": len(bvids)}}
            ]
        },
        "fetched_at": 0,
        "updated_at": 0,
    })


def _seed_success_task(ds, uid, endpoints):
    """Create a SUCCESS task in the store with given endpoints."""
    tv = TaskValue(uid=uid, status=TaskStatus.SUCCESS)
    for ep in endpoints:
        tv.endpoints[ep] = EndpointEntry(status=EndpointStatus.SUCCESS)
    return ds.put(f"uid:{uid}:task", tv.to_dict())


def _fake_videos_page(bvids, is_last=False, next_pn=None):
    """Build a FetchPageResult for videos endpoint."""
    return FetchPageResult(
        uid=0, endpoint="videos",
        raw_payload={
            "list": {"vlist": [{"bvid": bv} for bv in bvids]},
            "page": {"count": 100},
        },
        is_last_page=is_last,
        next_request={"pn": next_pn, "ps": 30} if next_pn else None,
    )


# ======================================================================
# Incremental mode — boundary detection
# ======================================================================

@pytest.mark.asyncio
async def test_incremental_boundary_hit_stops_early(stores, rl_ctl):
    """Stored IDs all appear on page 1 → boundary hit → one safety page → stop."""
    ds, es = stores

    await _seed_video_store(ds, 100, ["BV1", "BV2", "BV3", "BV4", "BV5"])
    await _seed_success_task(ds, 100, ["videos"])

    call_count = [0]

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        call_count[0] += 1
        pn = request_params.get("pn", 1)
        if pn == 1:
            # All IDs known → boundary
            return _fake_videos_page(["BV1", "BV2", "BV3", "BV4", "BV5"], next_pn=2)
        # Safety page (pn=2)
        return _fake_videos_page(["BV6", "BV7"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        100, endpoints=["videos"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    assert call_count[0] == 2  # boundary page + safety page

    # raw_payload.pages = only this run's pages (not old stored pages)
    stored = await ds.get(_fetch_key(100, "videos"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 2


@pytest.mark.asyncio
async def test_incremental_new_ids_continue_then_boundary(stores, rl_ctl):
    """Page 1 has new IDs → continue. Page 2 all known → boundary → safety."""
    ds, es = stores

    await _seed_video_store(ds, 101, ["BV1", "BV2", "BV3"])
    await _seed_success_task(ds, 101, ["videos"])

    call_log = []

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        pn = request_params.get("pn", 1)
        call_log.append(pn)
        if pn == 1:
            # BV1-3 known, BV4-6 new
            return _fake_videos_page(
                ["BV1", "BV2", "BV3", "BV4", "BV5", "BV6"], next_pn=2,
            )
        elif pn == 2:
            # All IDs now known → boundary
            return _fake_videos_page(["BV4", "BV5", "BV6"], next_pn=3)
        # Safety page (pn=3)
        return _fake_videos_page(["BV7"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        101, endpoints=["videos"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    assert call_log == [1, 2, 3]  # page 1 (new) → page 2 (boundary) → page 3 (safety)

    stored = await ds.get(_fetch_key(101, "videos"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 3


# ======================================================================
# Incremental mode — no stored data
# ======================================================================

@pytest.mark.asyncio
async def test_incremental_no_stored_data_fetches_all(stores, rl_ctl):
    """No stored data → known_ids=None → fetches all pages like first run."""
    ds, es = stores
    # Task exists but no fetch data for videos
    await _seed_success_task(ds, 102, ["videos"])

    call_log = []

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        pn = request_params.get("pn", 1)
        call_log.append(pn)
        if pn == 1:
            return _fake_videos_page(["BV1", "BV2"], next_pn=2)
        return _fake_videos_page(["BV3"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        102, endpoints=["videos"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    assert call_log == [1, 2]

    stored = await ds.get(_fetch_key(102, "videos"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 2


# ======================================================================
# Incremental mode — overwrite behaviour
# ======================================================================

@pytest.mark.asyncio
async def test_incremental_overwrites_stored_pages(stores, rl_ctl):
    """After incremental run, raw_payload.pages contains ONLY this run's pages."""
    ds, es = stores

    # Seed with 5 old pages
    old_pages = [
        {"list": {"vlist": [{"bvid": f"OLD{i}"}]}, "page": {"count": 5}}
        for i in range(5)
    ]
    await ds.put(_fetch_key(103, "videos"), {
        "uid": 103, "endpoint": "videos", "status": "SUCCESS",
        "raw_payload": {"pages": old_pages},
        "fetched_at": 0, "updated_at": 0,
    })
    await _seed_success_task(ds, 103, ["videos"])

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        # All IDs known → immediate boundary
        return _fake_videos_page(["OLD0", "OLD1"], next_pn=2)

    # is_last_page defaults to False in _fake_videos_page → safety page attempted
    # But the safety fetch will also return the same mock → that's fine
    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        103, endpoints=["videos"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS

    stored = await ds.get(_fetch_key(103, "videos"))
    pages = stored["raw_payload"]["pages"]
    # Should have boundary page + safety page, NOT the 5 old pages
    assert len(pages) == 2
    for page in pages:
        bvids = [v["bvid"] for v in page["list"]["vlist"]]
        assert all(not bv.startswith("OLD") or bv in ("OLD0", "OLD1") for bv in bvids)


# ======================================================================
# Full mode
# ======================================================================

@pytest.mark.asyncio
async def test_full_mode_overwrites_existing(stores, rl_ctl):
    """Full mode ignores existing data and overwrites."""
    ds, es = stores

    await _seed_video_store(ds, 104, ["OLD1", "OLD2", "OLD3"])
    await _seed_success_task(ds, 104, ["videos"])

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        pn = request_params.get("pn", 1)
        if pn == 1:
            return _fake_videos_page(["NEW1", "NEW2"], next_pn=2)
        return _fake_videos_page(["NEW3"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        104, endpoints=["videos"], mode="full",
    )

    assert result.status == TaskStatus.SUCCESS

    stored = await ds.get(_fetch_key(104, "videos"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 2
    all_bvids = []
    for page in pages:
        for v in page["list"]["vlist"]:
            all_bvids.append(v["bvid"])
    assert "OLD1" not in all_bvids
    assert all_bvids == ["NEW1", "NEW2", "NEW3"]


@pytest.mark.asyncio
async def test_full_mode_does_not_accumulate(stores, rl_ctl):
    """Running full mode twice does NOT accumulate pages."""
    ds, es = stores

    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        return _fake_videos_page(["A1", "A2"], is_last=True)

    await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch_1)).run_task(
        105, endpoints=["videos"], mode="full",
    )

    stored1 = await ds.get(_fetch_key(105, "videos"))
    assert len(stored1["raw_payload"]["pages"]) == 1

    # Second full run — should overwrite, NOT accumulate
    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        return _fake_videos_page(["B1"], is_last=True)

    await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch_2)).run_task(
        105, endpoints=["videos"], mode="full",
    )

    stored2 = await ds.get(_fetch_key(105, "videos"))
    pages = stored2["raw_payload"]["pages"]
    assert len(pages) == 1
    assert pages[0]["list"]["vlist"][0]["bvid"] == "B1"


@pytest.mark.asyncio
async def test_full_mode_resets_exhausted_task(stores, rl_ctl):
    """FAILED_EXHAUSTED task + full mode → resets all endpoints, re-runs."""
    ds, es = stores

    # Create a FAILED_EXHAUSTED task
    tv = TaskValue(uid=106, status=TaskStatus.FAILED_EXHAUSTED)
    tv.endpoints["videos"] = EndpointEntry(
        status=EndpointStatus.FAILED_EXHAUSTED, retry_count=3,
    )
    await ds.put("uid:106:task", tv.to_dict())

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        return _fake_videos_page(["NEW1"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        106, endpoints=["videos"], mode="full",
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.endpoints["videos"] == EndpointStatus.SUCCESS


# ======================================================================
# Mode switching — run_or_resume behaviour
# ======================================================================

@pytest.mark.asyncio
async def test_run_or_resume_success_incremental_enters_scan(stores, rl_ctl):
    """SUCCESS task + incremental mode → re-runs endpoints for scan."""
    ds, es = stores
    await _seed_video_store(ds, 107, ["BV1"])
    await _seed_success_task(ds, 107, ["videos"])

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        return _fake_videos_page(["BV1"], is_last=True)

    fetch_mock = AsyncMock(side_effect=fake_fetch)
    result = await Runner(ds, es, rl_ctl, fetch_fn=fetch_mock).run_or_resume(
        107, endpoints=["videos"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    # fetch_fn was actually called (not skipped)
    assert fetch_mock.called


@pytest.mark.asyncio
async def test_run_or_resume_success_full_triggers_refetch(stores, rl_ctl):
    """SUCCESS task + full mode → run_task (fresh=True) → full re-fetch."""
    ds, es = stores
    await _seed_video_store(ds, 108, ["OLD1"])
    await _seed_success_task(ds, 108, ["videos"])

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        return _fake_videos_page(["NEW1", "NEW2"], is_last=True)

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        108, endpoints=["videos"], mode="full",
    )

    assert result.status == TaskStatus.SUCCESS
    stored = await ds.get(_fetch_key(108, "videos"))
    bvids = [v["bvid"] for v in stored["raw_payload"]["pages"][0]["list"]["vlist"]]
    assert bvids == ["NEW1", "NEW2"]


@pytest.mark.asyncio
async def test_run_or_resume_failed_permanent_both_modes(stores, rl_ctl):
    """FAILED_PERMANENT task is NOT re-run in either mode."""
    ds, es = stores

    tv = TaskValue(uid=109, status=TaskStatus.FAILED_PERMANENT)
    tv.endpoints["videos"] = EndpointEntry(status=EndpointStatus.FAILED_PERMANENT)
    await ds.put("uid:109:task", tv.to_dict())

    fetch_mock = AsyncMock(return_value=_fake_videos_page(["X"], is_last=True))
    r1 = await Runner(ds, es, rl_ctl, fetch_fn=fetch_mock).run_or_resume(
        109, endpoints=["videos"], mode="incremental",
    )
    assert r1.status == TaskStatus.FAILED_PERMANENT

    r2 = await Runner(ds, es, rl_ctl, fetch_fn=fetch_mock).run_or_resume(
        109, endpoints=["videos"], mode="full",
    )
    assert r2.status == TaskStatus.FAILED_PERMANENT

    # fetch_fn was never called
    assert not fetch_mock.called


# ======================================================================
# Incremental mode — non-paginated endpoint
# ======================================================================

@pytest.mark.asyncio
async def test_incremental_non_paginated_overwrites(stores, rl_ctl):
    """Non-paginated endpoint (user_info) in incremental mode simply overwrites."""
    ds, es = stores

    # Seed old user_info data
    await ds.put(_fetch_key(110, "user_info"), {
        "uid": 110, "endpoint": "user_info", "status": "SUCCESS",
        "raw_payload": {"name": "old_name"},
        "fetched_at": 0, "updated_at": 0,
    })
    await _seed_success_task(ds, 110, ["user_info"])

    result = await Runner(
        ds, es, rl_ctl,
        fetch_fn=AsyncMock(return_value=_fake_page(110, {"name": "new_name"})),
    ).run_or_resume(110, endpoints=["user_info"], mode="incremental")

    assert result.status == TaskStatus.SUCCESS
    stored = await ds.get(_fetch_key(110, "user_info"))
    assert stored["raw_payload"] == {"name": "new_name"}


# ======================================================================
# Command mode passthrough
# ======================================================================

@pytest.mark.asyncio
async def test_command_mode_passthrough(stores, rl_ctl):
    """Mode parameter correctly passes from command → runner."""
    from bili_unit.fetching.command import Command

    ds, es = stores

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        return _fake_page(uid, {"ok": True})

    cmd = Command(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch))
    r1 = await cmd.fetch_uid(111, endpoints=["user_info"], mode="incremental")
    assert r1.status == TaskStatus.SUCCESS

    r2 = await cmd.fetch_uid(111, endpoints=["user_info"], mode="full")
    assert r2.status == TaskStatus.SUCCESS


# ======================================================================
# Bug: query returns PENDING for exhausted endpoint (subscribed_bangumi)
# ======================================================================

@pytest.mark.asyncio
async def test_query_endpoint_status_after_exhaustion(stores, rl_ctl, query):
    """After FetchingError exhausts retries, query must report FAILED_EXHAUSTED,
    not PENDING.

    Reproduces: subscribed_bangumi privacy error (53013) → 3 retries →
    task entry = FAILED_EXHAUSTED but query.get_endpoint() returned PENDING
    because it reads from _fetch_key (no data written on failure) instead
    of _task_key.
    """
    from bili_unit.fetching import RequestError

    ds, es = stores

    async def always_fail(uid, spec, credential, request_params, **kw):
        raise RequestError("53013: user privacy restricted")

    runner = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=always_fail))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        result = await runner.run_task(
            200, endpoints=["subscribed_bangumi"],
        )

    # runner result should reflect exhaustion
    assert result.status == TaskStatus.FAILED_EXHAUSTED
    assert result.endpoints["subscribed_bangumi"] == EndpointStatus.FAILED_EXHAUSTED

    # query must agree — this is the bug: it was returning PENDING
    dto = await query.get_endpoint(200, "subscribed_bangumi")
    assert dto is not None
    assert dto.status == EndpointStatus.FAILED_EXHAUSTED


# ======================================================================
# Incremental mode — anchor pagination (upower_qa)
# ======================================================================

@pytest.mark.asyncio
async def test_incremental_anchor_pagination_boundary(stores, rl_ctl):
    """Anchor-paginated endpoint (upower_qa) detects boundary in incremental mode.

    Verifies that item_id_path="list[*].qa_id" works correctly when pages
    are stored in raw_payload.pages and new pages use anchor cursor.
    """
    ds, es = stores

    # Seed stored data: previous run captured qa_ids 101, 102, 103
    await ds.put(_fetch_key(300, "upower_qa"), {
        "uid": 300, "endpoint": "upower_qa", "status": "SUCCESS",
        "raw_payload": {
            "pages": [
                {"list": [{"qa_id": 101}, {"qa_id": 102}, {"qa_id": 103}], "anchor": 0},
            ],
        },
        "fetched_at": 0, "updated_at": 0,
    })
    await _seed_success_task(ds, 300, ["upower_qa"])

    # Mock: first page returns all known IDs → boundary hit
    # Second page (safety) also returns known IDs
    call_log = []

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        anchor = request_params.get("anchor", 0)
        call_log.append(anchor)
        if anchor == 0:
            # First page: all known IDs → boundary
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 101}, {"qa_id": 102}], "anchor": 200},
                is_last_page=False, next_request={"anchor": 200},
            )
        # Safety page
        return FetchPageResult(
            uid=uid, endpoint="upower_qa",
            raw_payload={"list": [{"qa_id": 103}], "anchor": 0},
            is_last_page=True, next_request=None,
        )

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        300, endpoints=["upower_qa"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    # Should have fetched exactly 2 pages (boundary + safety)
    assert call_log == [0, 200]
    # Stored pages should be only this run's pages
    stored = await ds.get(_fetch_key(300, "upower_qa"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 2


@pytest.mark.asyncio
async def test_incremental_anchor_pagination_new_ids(stores, rl_ctl):
    """Anchor pagination finds new IDs, continues until boundary."""
    ds, es = stores

    # Seed stored data: known qa_ids 101, 102
    await ds.put(_fetch_key(301, "upower_qa"), {
        "uid": 301, "endpoint": "upower_qa", "status": "SUCCESS",
        "raw_payload": {
            "pages": [
                {"list": [{"qa_id": 101}, {"qa_id": 102}], "anchor": 0},
            ],
        },
        "fetched_at": 0, "updated_at": 0,
    })
    await _seed_success_task(ds, 301, ["upower_qa"])

    call_log = []

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        anchor = request_params.get("anchor", 0)
        call_log.append(anchor)
        if anchor == 0:
            # Page 1: mix of known + new → continue
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 101}, {"qa_id": 201}], "anchor": 201},
                is_last_page=False, next_request={"anchor": 201},
            )
        elif anchor == 201:
            # Page 2: all known (101 and 201 now known) → boundary
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 201}], "anchor": 301},
                is_last_page=False, next_request={"anchor": 301},
            )
        # Safety page
        return FetchPageResult(
            uid=uid, endpoint="upower_qa",
            raw_payload={"list": [{"qa_id": 301}], "anchor": 0},
            is_last_page=True, next_request=None,
        )

    result = await Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch)).run_or_resume(
        301, endpoints=["upower_qa"], mode="incremental",
    )

    assert result.status == TaskStatus.SUCCESS
    assert call_log == [0, 201, 301]
    stored = await ds.get(_fetch_key(301, "upower_qa"))
    pages = stored["raw_payload"]["pages"]
    assert len(pages) == 3


# ======================================================================
# runner — ResourceUnavailableError handling (no retry, no fan-out abort)
# ======================================================================

@pytest.mark.asyncio
async def test_runner_resource_unavailable_skips_retry(stores, rl_ctl):
    """uid-level endpoint: ResourceUnavailableError → FAILED_PERMANENT, no retries."""
    ds, es = stores
    attempts = [0]

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        attempts[0] += 1
        raise ResourceUnavailableError("subscribed_bangumi code=53013: privacy")

    runner = Runner(ds, es, rl_ctl, fetch_fn=AsyncMock(side_effect=fake_fetch))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        result = await runner.run_task(701, endpoints=["subscribed_bangumi"])

    # Only one call — runner did NOT consume the retry budget.
    assert attempts[0] == 1
    assert result.endpoints["subscribed_bangumi"] == EndpointStatus.FAILED_PERMANENT
    assert result.status == TaskStatus.FAILED_PERMANENT


@pytest.mark.asyncio
async def test_item_fanout_resource_unavailable_only_skips_one_item(stores, rl_ctl):
    """item-level fan-out: ResourceUnavailableError fails the single item only,
    sibling items continue to succeed."""
    ds, es = stores

    from bili_unit.fetching.keys import _fetch_key, _item_fetch_key, _task_key
    from bili_unit.fetching.task import EndpointEntry, TaskValue

    uid = 702
    tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
    tv.endpoints["video_detail"] = EndpointEntry(status=EndpointStatus.PENDING)
    await ds.put(_task_key(uid), tv.to_dict())
    await ds.put(_fetch_key(uid, "videos"), {
        "uid": uid,
        "endpoint": "videos",
        "status": "SUCCESS",
        "raw_payload": {
            "pages": [
                {"list": {"vlist": [{"bvid": "BV_ok"}, {"bvid": "BV_dead"}]}},
            ],
        },
    })

    call_log = []

    async def fake_item(item_id, credential, **kw):
        call_log.append(item_id)
        if item_id == "BV_dead":
            raise ResourceUnavailableError(
                f"video_detail[{item_id}]: code=53013",
            )
        return {"info": {"bvid": item_id}, "tags": []}

    spec = get_endpoint("video_detail")
    assert spec is not None

    runner = Runner(ds, es, rl_ctl)
    with patch.object(spec, "callable", fake_item), patch(
        "bili_unit._retry.asyncio.sleep", new=AsyncMock(),
    ):
        await runner._run_item_endpoint(uid, spec, credential=None, mode="full")

    # BV_dead failed exactly once (no retries); BV_ok succeeded.
    assert call_log.count("BV_dead") == 1
    assert "BV_ok" in call_log
    assert call_log.count("BV_ok") == 1

    # video_detail status is PARTIAL_ITEM (1 success, 1 failure).
    task_d = await ds.get(_task_key(uid))
    assert (
        task_d["endpoints"]["video_detail"]["status"]
        == EndpointStatus.PARTIAL_ITEM.value
    )

    # The successful item is stored.
    ok = await ds.get(_item_fetch_key(uid, "video_detail", "BV_ok"))
    assert ok is not None
    assert ok["status"] == "SUCCESS"

    # Failed item is NOT stored.
    dead = await ds.get(_item_fetch_key(uid, "video_detail", "BV_dead"))
    assert dead is None

    # Error record carries retryable=false.
    errs = await es.list_by_uid(uid)
    dead_errs = [e for e in errs if (e.detail or {}).get("item_id") == "BV_dead"]
    assert len(dead_errs) == 1
    assert dead_errs[0].retryable is False


# ---------------------------------------------------------------------------
# Stale RUNNING takeover (issue #3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_running_task_within_threshold_is_rejected(stores, rl_ctl):
    """Fresh RUNNING task (recent updated_at) → returns RUNNING, no work done."""
    import time as _time

    from bili_unit.fetching.keys import _task_key

    ds, es = stores
    uid = 9001

    now_ms = int(_time.time() * 1000)
    tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
    tv.updated_at = now_ms - 60_000  # 1 minute ago — fresh
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.RUNNING)
    await ds.put(_task_key(uid), tv.to_dict())

    runner = Runner(ds, es, rl_ctl, stale_running_threshold_ms=15 * 60 * 1000)
    result = await runner.run_or_resume(uid, endpoints=["user_info"])

    assert result.status == TaskStatus.RUNNING
    # Endpoint not touched — still RUNNING in store
    tv2 = TaskValue.from_dict(await ds.get(_task_key(uid)))
    assert tv2.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_running_task_past_threshold_is_taken_over(stores, rl_ctl, monkeypatch):
    """Stale RUNNING task (updated_at older than threshold) → resumes as PARTIAL."""
    import time as _time

    from bili_unit.fetching.keys import _task_key

    ds, es = stores
    uid = 9002

    # Seed the task as RUNNING — the KV store will stamp updated_at with now.
    tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.PENDING)
    await ds.put(_task_key(uid), tv.to_dict())

    # Advance the clock so that the runner sees the task as 30 minutes old.
    # We patch time.time in both runner and _storage._kv to avoid split-brain.
    future_time = _time.time() + 30 * 60  # 30 minutes in the future

    async def fake_fetch(uid_arg, spec, credential, request_params, **kw):
        return FetchPageResult(
            uid=uid_arg, endpoint="user_info",
            raw_payload={"name": "ok"},
            is_last_page=True, next_request=None,
        )

    monkeypatch.setattr("bili_unit.fetching.runner.time.time", lambda: future_time)
    monkeypatch.setattr("bili_unit._storage._kv.time.time", lambda: future_time)

    runner = Runner(ds, es, rl_ctl, stale_running_threshold_ms=15 * 60 * 1000, fetch_fn=fake_fetch)
    result = await runner.run_or_resume(uid, endpoints=["user_info"])

    # Stale RUNNING was taken over — final status reflects actual run, not RUNNING-frozen
    assert result.status != TaskStatus.RUNNING
    # Endpoint actually ran
    tv2 = TaskValue.from_dict(await ds.get(_task_key(uid)))
    assert tv2.endpoints["user_info"].status == EndpointStatus.SUCCESS


@pytest.mark.asyncio
async def test_endpoint_completion_heartbeats_task_updated_at(stores, rl_ctl):
    """update_task_endpoint refreshes task.updated_at so long-running tasks
    don't get falsely flagged as stale."""
    import time as _time

    from bili_unit.fetching.keys import _task_key

    ds, es = stores
    uid = 9003

    initial_ts = int(_time.time() * 1000) - 10_000  # 10s ago
    tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
    tv.created_at = initial_ts
    tv.updated_at = initial_ts
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.PENDING)
    await ds.put(_task_key(uid), tv.to_dict())

    # Trigger a single endpoint update — should bump task.updated_at
    await ds.update_task_endpoint(
        _task_key(uid), "user_info", "SUCCESS",
        retry_count=0, last_error_id=None,
    )

    tv2 = TaskValue.from_dict(await ds.get(_task_key(uid)))
    assert tv2.updated_at is not None
    assert tv2.updated_at > initial_ts


@pytest.mark.asyncio
async def test_running_task_with_missing_updated_at_is_treated_as_stale(
    stores, rl_ctl, monkeypatch,
):
    """Defensive: tv.updated_at == None → age_ms huge → stale → takeover.

    The KV store always stamps updated_at on put(), so we simulate a missing
    updated_at by patching the runner's time.time to return a value far enough
    in the future that even a brand-new task looks stale.
    """
    import time as _time

    from bili_unit.fetching.keys import _task_key

    ds, es = stores
    uid = 9004

    tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.PENDING)
    await ds.put(_task_key(uid), tv.to_dict())

    # Set runner clock 30 minutes ahead so any recently-stored task looks stale.
    future_time = _time.time() + 30 * 60

    async def fake_fetch(uid_arg, spec, credential, request_params, **kw):
        return FetchPageResult(
            uid=uid_arg, endpoint="user_info",
            raw_payload={"name": "ok"},
            is_last_page=True, next_request=None,
        )

    monkeypatch.setattr("bili_unit.fetching.runner.time.time", lambda: future_time)
    monkeypatch.setattr("bili_unit._storage._kv.time.time", lambda: future_time)

    runner = Runner(ds, es, rl_ctl, stale_running_threshold_ms=15 * 60 * 1000, fetch_fn=fake_fetch)
    result = await runner.run_or_resume(uid, endpoints=["user_info"])
    assert result.status != TaskStatus.RUNNING

