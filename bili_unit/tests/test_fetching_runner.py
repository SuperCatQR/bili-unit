# tests for bili_unit.fetching.runner (Phase 6 rewrite for SQLite stack).

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import (
    AuthError,
    EndpointStatus,
    Http412Error,
    ResourceUnavailableError,
    TaskStatus,
)
from bili_unit.fetching._bilibili_adapter import FetchPageResult
from bili_unit.fetching._endpoint_catalog import get_endpoint
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.runner import Runner, _extract_item_ids
from bili_unit.observability import RunContext, RunReporter, SqliteSink

# ---------------------------------------------------------------------------
# Helpers (intentionally inline — conftest.py is frozen during Phase 6).
# ---------------------------------------------------------------------------

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


def _runner(store: FetchingStore, settings: BiliSettings, *, fetch_fn=None, **kw) -> Runner:
    return Runner(store, _fast_rate_limit(), settings, fetch_fn=fetch_fn, **kw)


async def _list_stage_events(ctx: UidContext) -> list[dict]:
    rows = await ctx.conn.fetch_all(
        "SELECT event, level, stage, endpoint, item_type, item_id, data_json "
        "FROM stage_event ORDER BY id",
    )
    return [
        {
            "event": row["event"],
            "level": row["level"],
            "stage": row["stage"],
            "endpoint": row["endpoint"],
            "item_type": row["item_type"],
            "item_id": row["item_id"],
            "data": json.loads(row["data_json"] or "{}"),
        }
        for row in rows
    ]


async def _list_stage_runs(ctx: UidContext) -> list[dict]:
    rows = await ctx.conn.fetch_all(
        "SELECT command, status, args_json, summary_json "
        "FROM stage_run ORDER BY started_at_ms",
    )
    return [
        {
            "command": row["command"],
            "status": row["status"],
            "args": json.loads(row["args_json"] or "{}"),
            "summary": json.loads(row["summary_json"] or "{}"),
        }
        for row in rows
    ]


def _reporter(ctx: UidContext, uid: int, **args) -> RunReporter:
    run_context = RunContext.create(uid=uid, command="fetch", args=args)
    return RunReporter(run_context, SqliteSink(ctx.conn))


class _NullProgress:
    def __init__(self, *, total: int, label: str, **kwargs) -> None:
        self.total = total
        self.label = label
        self.kwargs = kwargs
        self.updates: list[tuple[int, str | None]] = []
        self.closed = False

    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def update(self, n: int = 1, *, postfix: str | None = None) -> None:
        self.updates.append((n, postfix))

    def close(self) -> None:
        self.closed = True


def _fake_page(uid: int, payload: dict, *, endpoint: str = "user_info") -> FetchPageResult:
    return FetchPageResult(
        uid=uid, endpoint=endpoint, raw_payload=payload,
        is_last_page=True, next_request=None,
    )


def _fake_videos_page(
    bvids: list[str],
    *,
    is_last: bool = False,
    next_pn: int | None = None,
    total_count: int | None = None,
) -> FetchPageResult:
    return FetchPageResult(
        uid=0, endpoint="videos",
        raw_payload={
            "list": {"vlist": [{"bvid": bv} for bv in bvids]},
            "page": {"count": total_count if total_count is not None else len(bvids)},
        },
        is_last_page=is_last,
        next_request={"pn": next_pn, "ps": 30} if next_pn else None,
    )


async def _seed_videos_payload(store: FetchingStore, bvids: list[str], *, ep: str = "videos") -> None:
    """Seed an endpoint with a single-page payload and SUCCESS state.

    Marks the parent task as SUCCESS so that subsequent run_or_resume calls
    enter the "incremental scan" branch (not the resume branch which would
    reset the seeded endpoint).
    """
    await store.init_task([ep])
    await store.save_raw_payload(ep, "", {
        "pages": [
            {
                "list": {"vlist": [{"bvid": bv} for bv in bvids]},
                "page": {"count": len(bvids)},
            },
        ],
    })
    await store.update_endpoint_state(ep, status=EndpointStatus.SUCCESS.value)
    await store.update_task_status(TaskStatus.SUCCESS.value)


# ======================================================================
# runner — single endpoint success
# ======================================================================

async def test_runner_single_success(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 1)
    try:
        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(return_value=_fake_page(1, {"ok": True})),
        )
        result = await runner.run_task(1, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS

        assert await store.get_endpoint_status("user_info") == EndpointStatus.SUCCESS.value
        assert await store.get_raw_payload("user_info") == {"ok": True}
    finally:
        await ctx.close()


# ======================================================================
# runner — partial failure (one success, one permanent fail)
# ======================================================================

async def test_runner_partial(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 2)
    try:
        async def fake_fetch(uid, spec, credential, request_params, **kw):
            if spec.name == "user_info":
                return _fake_page(uid, {"ok": True})
            if spec.name == "videos":
                raise Http412Error("412 videos")
            raise RuntimeError(spec.name)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_task(2, endpoints=["user_info", "videos"])

        assert result.status == TaskStatus.PARTIAL
        assert result.endpoints.get("user_info") == EndpointStatus.SUCCESS
        assert result.endpoints.get("videos") in (
            EndpointStatus.FAILED_EXHAUSTED,
            EndpointStatus.FAILED_RETRYABLE,
        )
    finally:
        await ctx.close()


# ======================================================================
# runner — credential-required endpoints fail without blocking public endpoints
# ======================================================================

async def test_runner_public_endpoint_does_not_get_credential(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 3)
    try:
        fetch_mock = AsyncMock(return_value=_fake_page(3, {"ok": True}))
        runner = _runner(store, _settings(tmp_path), fetch_fn=fetch_mock)
        get_credential_mock = AsyncMock(side_effect=AuthError("no sessdata"))
        with patch(
            "bili_unit.fetching.runner.get_credential",
            new=get_credential_mock,
        ):
            result = await runner.run_task(3, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS
        assert get_credential_mock.await_count == 0
        assert fetch_mock.await_args.args[2] is None
    finally:
        await ctx.close()


async def test_runner_credential_required_auth_failure_is_permanent(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 3003)
    try:
        runner = _runner(store, _settings(tmp_path))
        with patch(
            "bili_unit.fetching.runner.get_credential",
            new=AsyncMock(side_effect=AuthError("no sessdata")),
        ):
            result = await runner.run_task(3003, endpoints=["user_medal"])
        assert result.status == TaskStatus.FAILED_PERMANENT
        assert result.endpoints["user_medal"] == EndpointStatus.FAILED_PERMANENT
        errors = await store.list_errors()
        assert any(e["error_type"] == "AuthError" for e in errors)
    finally:
        await ctx.close()


# ======================================================================
# runner — 412 retry -> recover
# ======================================================================

async def test_runner_412_retry_eventually_succeeds(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 4)
    try:
        attempts = [0]

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            attempts[0] += 1
            if attempts[0] <= 2:
                raise Http412Error("too fast")
            return _fake_page(uid, {"ok": True})

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_task(4, endpoints=["user_info"])

        assert result.status == TaskStatus.SUCCESS
        assert attempts[0] == 3  # 2 fails + 1 success
        assert await store.get_endpoint_status("user_info") == EndpointStatus.SUCCESS.value
    finally:
        await ctx.close()


# ======================================================================
# runner — progress resumption (videos multi-page with mid-run failure)
# ======================================================================

async def test_runner_progress_resumption(tmp_path: Path):
    """First run: page 1 succeeds, page 2 fails -> progress saved at page 2.
    Second run: resume from page 2 cursor and finish.
    """
    ctx, store = await _open_store(tmp_path, 5)
    try:
        call_log: list[tuple[str, int]] = []

        async def fake_fetch_1(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            call_log.append(("run1", pn))
            if pn == 1:
                return FetchPageResult(
                    uid=uid, endpoint="videos",
                    raw_payload={
                        "list": {"vlist": [{"bvid": f"BV{i}"} for i in range(30)]},
                        "page": {"count": 65},
                    },
                    is_last_page=False,
                    next_request={"pn": 2, "ps": 30},
                )
            raise Http412Error("412 page 2")

        runner1 = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch_1),
        )
        result1 = await runner1.run_task(5, endpoints=["videos"])
        assert result1.status in (TaskStatus.FAILED_EXHAUSTED, TaskStatus.FAILED_RETRYABLE)

        # Progress was persisted with cursor for page 2.
        progress = await store.get_progress("videos")
        assert progress is not None
        assert progress["cursor"] == {"pn": 2, "ps": 30}
        stored_after_page_1 = await store.get_raw_payload("videos")
        assert stored_after_page_1 is not None
        assert [
            item["bvid"]
            for item in stored_after_page_1["pages"][0]["list"]["vlist"]
        ] == [f"BV{i}" for i in range(30)]

        # Second run — must start from page 2.
        async def fake_fetch_2(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            call_log.append(("run2", pn))
            return FetchPageResult(
                uid=uid, endpoint="videos",
                raw_payload={
                    "list": {"vlist": [{"bvid": "BV999"}]},
                    "page": {"count": 65},
                },
                is_last_page=True,
                next_request=None,
            )

        runner2 = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch_2),
        )
        # FAILED_EXHAUSTED -> resume_task (run_or_resume's else branch).
        await runner2.run_or_resume(5, endpoints=["videos"])

        assert ("run2", 2) in call_log
    finally:
        await ctx.close()


# ======================================================================
# _extract_item_ids — pure function tests
# ======================================================================

def test_extract_item_ids_videos_shape():
    payload = {
        "list": {"vlist": [{"bvid": "BV111"}, {"bvid": "BV222"}]},
        "page": {"count": 2},
    }
    assert _extract_item_ids(payload, "list.vlist[*].bvid") == ["BV111", "BV222"]


def test_extract_item_ids_dynamics_shape():
    payload = {
        "items": [
            {"id_str": "111", "type": "DYNAMIC_TYPE_AV"},
            {"id_str": "222", "type": "DYNAMIC_TYPE_DRAW"},
        ],
        "has_more": 0,
    }
    assert _extract_item_ids(payload, "items[*].id_str") == ["111", "222"]


def test_extract_item_ids_none_path():
    assert _extract_item_ids({"any": "data"}, None) == []


def test_extract_item_ids_missing_key():
    assert _extract_item_ids({"list": {"other": []}}, "list.vlist[*].bvid") == []


def test_extract_item_ids_wrong_type_at_expand():
    assert _extract_item_ids({"list": {"vlist": "not a list"}}, "list.vlist[*].bvid") == []


def test_extract_item_ids_empty_list():
    assert _extract_item_ids({"items": []}, "items[*].id_str") == []


def test_extract_item_ids_no_expand():
    assert _extract_item_ids({"meta": {"id": 42}}, "meta.id") == ["42"]


def test_extract_item_ids_expand_is_last_segment():
    assert _extract_item_ids({"tags": ["a", "b", "c"]}, "tags[*]") == ["a", "b", "c"]


# ======================================================================
# Incremental mode — boundary detection
# ======================================================================

async def test_incremental_boundary_hit_stops_early(tmp_path: Path):
    """Stored IDs all appear on page 1 -> boundary hit -> one safety page -> stop."""
    ctx, store = await _open_store(tmp_path, 100)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3", "BV4", "BV5"])

        call_count = [0]

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            call_count[0] += 1
            pn = request_params.get("pn", 1)
            if pn == 1:
                # All IDs known -> boundary
                return _fake_videos_page(["BV1", "BV2", "BV3", "BV4", "BV5"], next_pn=2)
            return _fake_videos_page(["BV6", "BV7"], is_last=True)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            100, endpoints=["videos"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_count[0] == 2  # boundary page + safety page

        stored = await store.get_raw_payload("videos")
        assert stored is not None
        # raw_payload.pages = only this run's pages.
        assert len(stored["pages"]) == 2
    finally:
        await ctx.close()


async def test_incremental_backfills_incomplete_page_listing(tmp_path: Path):
    """If stored pages are only a prefix, page-1 boundary must not stop scan."""
    ctx, store = await _open_store(tmp_path, 112)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            call_log.append(pn)
            if pn == 1:
                return _fake_videos_page(
                    ["BV1", "BV2"],
                    next_pn=2,
                    total_count=5,
                )
            if pn == 2:
                return _fake_videos_page(
                    ["BV3", "BV4"],
                    next_pn=3,
                    total_count=5,
                )
            return _fake_videos_page(["BV5"], is_last=True, total_count=5)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            112, endpoints=["videos"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [1, 2, 3]
        stored = await store.get_raw_payload("videos")
        assert stored is not None
        stored_bvids = [
            item["bvid"]
            for page in stored["pages"]
            for item in page["list"]["vlist"]
        ]
        assert stored_bvids == ["BV1", "BV2", "BV3", "BV4", "BV5"]
    finally:
        await ctx.close()


async def test_incremental_backfills_incomplete_cursor_listing(tmp_path: Path):
    """Cursor endpoints with stored has_more=True must continue past boundary."""
    ctx, store = await _open_store(tmp_path, 113)
    try:
        await store.init_task(["opus"])
        await store.save_raw_payload("opus", "", {
            "pages": [
                {
                    "items": [{"opus_id": "O1"}, {"opus_id": "O2"}],
                    "has_more": True,
                    "offset": "old-next",
                },
            ],
        })
        await store.update_endpoint_state("opus", status=EndpointStatus.SUCCESS.value)
        await store.update_task_status(TaskStatus.SUCCESS.value)

        spec = get_endpoint("opus")
        assert spec is not None
        call_log: list[str] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            offset = request_params.get("offset", "")
            call_log.append(offset)
            if offset == "":
                return FetchPageResult(
                    uid=uid,
                    endpoint="opus",
                    raw_payload={
                        "items": [{"opus_id": "O1"}, {"opus_id": "O2"}],
                        "has_more": True,
                        "offset": "next-1",
                    },
                    is_last_page=False,
                    next_request={"offset": "next-1"},
                )
            return FetchPageResult(
                uid=uid,
                endpoint="opus",
                raw_payload={
                    "items": [{"opus_id": "O3"}],
                    "has_more": False,
                    "offset": "",
                },
                is_last_page=True,
                next_request=None,
            )

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            113, endpoints=["opus"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == ["", "next-1"]
        stored = await store.get_raw_payload("opus")
        assert stored is not None
        assert len(stored["pages"]) == 2
    finally:
        await ctx.close()


async def test_incremental_new_ids_continue_then_boundary(tmp_path: Path):
    """Page 1 has new IDs -> continue. Page 2 all known -> boundary -> safety."""
    ctx, store = await _open_store(tmp_path, 101)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2", "BV3"])

        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            call_log.append(pn)
            if pn == 1:
                return _fake_videos_page(
                    ["BV1", "BV2", "BV3", "BV4", "BV5", "BV6"], next_pn=2,
                )
            elif pn == 2:
                return _fake_videos_page(["BV4", "BV5", "BV6"], next_pn=3)
            return _fake_videos_page(["BV7"], is_last=True)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            101, endpoints=["videos"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [1, 2, 3]  # new -> boundary -> safety

        stored = await store.get_raw_payload("videos")
        assert stored is not None
        stored_bvids = [
            item["bvid"]
            for page in stored["pages"]
            for item in page["list"]["vlist"]
        ]
        assert stored_bvids == ["BV1", "BV2", "BV3", "BV4", "BV5", "BV6", "BV7"]
    finally:
        await ctx.close()


# ======================================================================
# Incremental mode — no stored data
# ======================================================================

async def test_incremental_no_stored_data_fetches_all(tmp_path: Path):
    """No stored data -> known_ids=None -> fetches all pages like first run."""
    ctx, store = await _open_store(tmp_path, 102)
    try:
        # task SUCCESS but no fetch data for videos (simulate task seeded but
        # endpoint never ran).
        await store.init_task(["videos"])
        await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
        await store.update_task_status(TaskStatus.SUCCESS.value)

        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            call_log.append(pn)
            if pn == 1:
                return _fake_videos_page(["BV1", "BV2"], next_pn=2)
            return _fake_videos_page(["BV3"], is_last=True)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            102, endpoints=["videos"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [1, 2]

        stored = await store.get_raw_payload("videos")
        assert stored is not None
        assert len(stored["pages"]) == 2
    finally:
        await ctx.close()


# ======================================================================
# Incremental mode — overwrite behaviour
# ======================================================================

async def test_incremental_preserves_unseen_stored_pages(tmp_path: Path):
    """Incremental keeps old pages whose items were not seen this run."""
    ctx, store = await _open_store(tmp_path, 103)
    try:
        # Seed with 5 old pages.
        old_pages = [
            {"list": {"vlist": [{"bvid": f"OLD{i}"}]}, "page": {"count": 5}}
            for i in range(5)
        ]
        await store.init_task(["videos"])
        await store.save_raw_payload("videos", "", {"pages": old_pages})
        await store.update_endpoint_state(
            "videos", status=EndpointStatus.SUCCESS.value,
        )
        await store.update_task_status(TaskStatus.SUCCESS.value)

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["OLD0", "OLD1"], next_pn=2)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            103, endpoints=["videos"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        stored = await store.get_raw_payload("videos")
        assert stored is not None
        stored_bvids = [
            item["bvid"]
            for page in stored["pages"]
            for item in page["list"]["vlist"]
        ]
        assert stored_bvids == ["OLD0", "OLD1", "OLD2", "OLD3", "OLD4"]
    finally:
        await ctx.close()


# ======================================================================
# Full mode
# ======================================================================

async def test_full_mode_overwrites_existing(tmp_path: Path):
    """Full mode ignores existing data and overwrites."""
    ctx, store = await _open_store(tmp_path, 104)
    try:
        await _seed_videos_payload(store, ["OLD1", "OLD2", "OLD3"])

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            pn = request_params.get("pn", 1)
            if pn == 1:
                return _fake_videos_page(["NEW1", "NEW2"], next_pn=2)
            return _fake_videos_page(["NEW3"], is_last=True)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            104, endpoints=["videos"], mode="full",
        )

        assert result.status == TaskStatus.SUCCESS
        stored = await store.get_raw_payload("videos")
        assert stored is not None
        all_bvids: list[str] = []
        for page in stored["pages"]:
            for v in page["list"]["vlist"]:
                all_bvids.append(v["bvid"])
        assert "OLD1" not in all_bvids
        assert all_bvids == ["NEW1", "NEW2", "NEW3"]
    finally:
        await ctx.close()


async def test_full_mode_does_not_accumulate(tmp_path: Path):
    """Running full mode twice does NOT accumulate pages."""
    ctx, store = await _open_store(tmp_path, 105)
    try:
        async def fake_fetch_1(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["A1", "A2"], is_last=True)

        await _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch_1),
        ).run_task(105, endpoints=["videos"], mode="full")

        stored1 = await store.get_raw_payload("videos")
        assert stored1 is not None
        assert len(stored1["pages"]) == 1

        # Second full run — overwrites.
        async def fake_fetch_2(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["B1"], is_last=True)

        await _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch_2),
        ).run_task(105, endpoints=["videos"], mode="full")

        stored2 = await store.get_raw_payload("videos")
        assert stored2 is not None
        assert len(stored2["pages"]) == 1
        assert stored2["pages"][0]["list"]["vlist"][0]["bvid"] == "B1"
    finally:
        await ctx.close()


async def test_full_mode_resets_exhausted_task(tmp_path: Path):
    """FAILED_EXHAUSTED task + full mode -> resets endpoints, re-runs."""
    ctx, store = await _open_store(tmp_path, 106)
    try:
        await store.init_task(["videos"])
        await store.update_endpoint_state(
            "videos", status=EndpointStatus.FAILED_EXHAUSTED.value, retry_count=3,
        )
        await store.update_task_status(TaskStatus.FAILED_EXHAUSTED.value)

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["NEW1"], is_last=True)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            106, endpoints=["videos"], mode="full",
        )

        assert result.status == TaskStatus.SUCCESS
        assert result.endpoints["videos"] == EndpointStatus.SUCCESS
    finally:
        await ctx.close()


# ======================================================================
# Mode switching — run_or_resume behaviour
# ======================================================================

async def test_run_or_resume_success_incremental_enters_scan(tmp_path: Path):
    """SUCCESS task + incremental mode -> re-runs endpoints for scan."""
    ctx, store = await _open_store(tmp_path, 107)
    try:
        await _seed_videos_payload(store, ["BV1"])

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["BV1"], is_last=True)

        fetch_mock = AsyncMock(side_effect=fake_fetch)
        result = await _runner(
            store, _settings(tmp_path), fetch_fn=fetch_mock,
        ).run_or_resume(107, endpoints=["videos"], mode="incremental")

        assert result.status == TaskStatus.SUCCESS
        assert fetch_mock.called  # not skipped
    finally:
        await ctx.close()


async def test_run_or_resume_success_full_triggers_refetch(tmp_path: Path):
    """SUCCESS task + full mode -> run_task (fresh=True) -> full re-fetch."""
    ctx, store = await _open_store(tmp_path, 108)
    try:
        await _seed_videos_payload(store, ["OLD1"])

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            return _fake_videos_page(["NEW1", "NEW2"], is_last=True)

        result = await _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        ).run_or_resume(108, endpoints=["videos"], mode="full")

        assert result.status == TaskStatus.SUCCESS
        stored = await store.get_raw_payload("videos")
        assert stored is not None
        bvids = [v["bvid"] for v in stored["pages"][0]["list"]["vlist"]]
        assert bvids == ["NEW1", "NEW2"]
    finally:
        await ctx.close()


async def test_run_or_resume_failed_permanent_both_modes(tmp_path: Path):
    """FAILED_PERMANENT task is NOT re-run in either mode."""
    ctx, store = await _open_store(tmp_path, 109)
    try:
        await store.init_task(["videos"])
        await store.update_endpoint_state(
            "videos", status=EndpointStatus.FAILED_PERMANENT.value,
        )
        await store.update_task_status(TaskStatus.FAILED_PERMANENT.value)

        fetch_mock = AsyncMock(return_value=_fake_videos_page(["X"], is_last=True))
        r1 = await _runner(
            store, _settings(tmp_path), fetch_fn=fetch_mock,
        ).run_or_resume(109, endpoints=["videos"], mode="incremental")
        assert r1.status == TaskStatus.FAILED_PERMANENT

        r2 = await _runner(
            store, _settings(tmp_path), fetch_fn=fetch_mock,
        ).run_or_resume(109, endpoints=["videos"], mode="full")
        assert r2.status == TaskStatus.FAILED_PERMANENT

        assert not fetch_mock.called
    finally:
        await ctx.close()


# ======================================================================
# Incremental mode — non-paginated endpoint
# ======================================================================

async def test_incremental_non_paginated_overwrites(tmp_path: Path):
    """Non-paginated endpoint (user_info) in incremental mode simply overwrites."""
    ctx, store = await _open_store(tmp_path, 110)
    try:
        await store.init_task(["user_info"])
        await store.save_raw_payload("user_info", "", {"name": "old_name"})
        await store.update_endpoint_state(
            "user_info", status=EndpointStatus.SUCCESS.value,
        )
        await store.update_task_status(TaskStatus.SUCCESS.value)

        result = await _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(return_value=_fake_page(110, {"name": "new_name"})),
        ).run_or_resume(110, endpoints=["user_info"], mode="incremental")

        assert result.status == TaskStatus.SUCCESS
        assert await store.get_raw_payload("user_info") == {"name": "new_name"}
    finally:
        await ctx.close()


# ======================================================================
# Command mode passthrough
# ======================================================================

async def test_command_mode_passthrough(tmp_path: Path):
    """Mode parameter passes from Command -> Runner; both modes succeed."""
    from bili_unit.fetching.command import Command

    settings = _settings(tmp_path)

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        return _fake_page(uid, {"ok": True})

    cmd = Command(
        settings, _fast_rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch),
    )
    r1 = await cmd.fetch_uid(111, endpoints=["user_info"], mode="incremental")
    assert r1.status == TaskStatus.SUCCESS

    r2 = await cmd.fetch_uid(111, endpoints=["user_info"], mode="full")
    assert r2.status == TaskStatus.SUCCESS


# ======================================================================
# Bug: endpoint status after exhaustion (subscribed_bangumi)
# ======================================================================

async def test_endpoint_status_after_exhaustion(tmp_path: Path):
    """After FetchingError exhausts retries, the store reports FAILED_EXHAUSTED.

    Originally reproduced as: subscribed_bangumi privacy error (53013) -> 3
    retries -> task entry = FAILED_EXHAUSTED but query.get_endpoint() returned
    PENDING because it read from the fetch key rather than the task. With the
    SQLite store the source of truth is fetch_endpoint_state, so this regression
    surface looks slightly different — we assert directly against that table.
    """
    from bili_unit.fetching import RequestError

    ctx, store = await _open_store(tmp_path, 200)
    try:
        async def always_fail(uid, spec, credential, request_params, **kw):
            raise RequestError("53013: user privacy restricted")

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=always_fail),
        )
        result = await runner.run_task(200, endpoints=["subscribed_bangumi"])

        assert result.status == TaskStatus.FAILED_EXHAUSTED
        assert result.endpoints["subscribed_bangumi"] == EndpointStatus.FAILED_EXHAUSTED

        status = await store.get_endpoint_status("subscribed_bangumi")
        assert status == EndpointStatus.FAILED_EXHAUSTED.value
    finally:
        await ctx.close()


# ======================================================================
# Incremental mode — anchor pagination (upower_qa)
# ======================================================================

async def test_incremental_anchor_pagination_boundary(tmp_path: Path):
    """Anchor-paginated endpoint detects boundary in incremental mode."""
    ctx, store = await _open_store(tmp_path, 300)
    try:
        await store.init_task(["upower_qa"])
        await store.save_raw_payload("upower_qa", "", {
            "pages": [
                {"list": [{"qa_id": 101}, {"qa_id": 102}, {"qa_id": 103}], "anchor": 0},
            ],
        })
        await store.update_endpoint_state(
            "upower_qa", status=EndpointStatus.SUCCESS.value,
        )
        await store.update_task_status(TaskStatus.SUCCESS.value)

        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            anchor = request_params.get("anchor", 0)
            call_log.append(anchor)
            if anchor == 0:
                return FetchPageResult(
                    uid=uid, endpoint="upower_qa",
                    raw_payload={"list": [{"qa_id": 101}, {"qa_id": 102}], "anchor": 200},
                    is_last_page=False, next_request={"anchor": 200},
                )
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 103}], "anchor": 0},
                is_last_page=True, next_request=None,
            )

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            300, endpoints=["upower_qa"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [0, 200]
        stored = await store.get_raw_payload("upower_qa")
        assert stored is not None
        assert len(stored["pages"]) == 2
    finally:
        await ctx.close()


async def test_incremental_anchor_pagination_new_ids(tmp_path: Path):
    """Anchor pagination finds new IDs, continues until boundary."""
    ctx, store = await _open_store(tmp_path, 301)
    try:
        await store.init_task(["upower_qa"])
        await store.save_raw_payload("upower_qa", "", {
            "pages": [
                {"list": [{"qa_id": 101}, {"qa_id": 102}], "anchor": 0},
            ],
        })
        await store.update_endpoint_state(
            "upower_qa", status=EndpointStatus.SUCCESS.value,
        )
        await store.update_task_status(TaskStatus.SUCCESS.value)

        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            anchor = request_params.get("anchor", 0)
            call_log.append(anchor)
            if anchor == 0:
                return FetchPageResult(
                    uid=uid, endpoint="upower_qa",
                    raw_payload={"list": [{"qa_id": 101}, {"qa_id": 201}], "anchor": 201},
                    is_last_page=False, next_request={"anchor": 201},
                )
            elif anchor == 201:
                return FetchPageResult(
                    uid=uid, endpoint="upower_qa",
                    raw_payload={"list": [{"qa_id": 201}], "anchor": 301},
                    is_last_page=False, next_request={"anchor": 301},
                )
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 301}], "anchor": 0},
                is_last_page=True, next_request=None,
            )

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_or_resume(
            301, endpoints=["upower_qa"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [0, 201, 301]
        stored = await store.get_raw_payload("upower_qa")
        assert stored is not None
        assert len(stored["pages"]) == 3
    finally:
        await ctx.close()


# ======================================================================
# runner — ResourceUnavailableError handling (no retry, no fan-out abort)
# ======================================================================

async def test_anchor_pagination_loop_stops_and_saves_pages(tmp_path: Path):
    """Repeated next_request stops pagination instead of looping forever."""
    ctx, store = await _open_store(tmp_path, 302)
    try:
        call_log: list[int] = []

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            anchor = request_params.get("anchor", 0)
            call_log.append(anchor)
            if anchor == 0:
                return FetchPageResult(
                    uid=uid, endpoint="upower_qa",
                    raw_payload={"list": [{"qa_id": 101}], "anchor": 100},
                    is_last_page=False, next_request={"anchor": 100},
                )
            if anchor == 100:
                return FetchPageResult(
                    uid=uid, endpoint="upower_qa",
                    raw_payload={"list": [{"qa_id": 102}], "anchor": 200},
                    is_last_page=False, next_request={"anchor": 200},
                )
            return FetchPageResult(
                uid=uid, endpoint="upower_qa",
                raw_payload={"list": [{"qa_id": 103}], "anchor": 100},
                is_last_page=False, next_request={"anchor": 100},
            )

        runner = _runner(
            store,
            _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
            reporter=_reporter(ctx, 302, mode="full", endpoints=["upower_qa"]),
        )
        result = await runner.run_task(302, endpoints=["upower_qa"])

        assert result.status == TaskStatus.SUCCESS
        assert call_log == [0, 100, 200]
        assert await store.get_endpoint_status("upower_qa") == EndpointStatus.SUCCESS.value

        stored = await store.get_raw_payload("upower_qa")
        assert stored is not None
        assert len(stored["pages"]) == 3

        progress = await store.get_progress("upower_qa")
        assert progress is not None
        assert progress["cursor"] is None

        events = await _list_stage_events(ctx)
        names = [event["event"] for event in events]
        assert names == [
            "fetch.run.started",
            "fetch.endpoint.started",
            "fetch.endpoint.page_saved",
            "fetch.endpoint.page_saved",
            "fetch.endpoint.page_saved",
            "fetch.endpoint.pagination_loop_detected",
            "fetch.endpoint.completed",
            "fetch.run.completed",
        ]
        page_saved = [
            event for event in events
            if event["event"] == "fetch.endpoint.page_saved"
        ]
        assert [event["data"]["page_count"] for event in page_saved] == [1, 2, 3]
        loop_event = next(
            event for event in events
            if event["event"] == "fetch.endpoint.pagination_loop_detected"
        )
        assert loop_event["level"] == "WARNING"
        assert loop_event["endpoint"] == "upower_qa"
        assert loop_event["data"]["request_params"] == {"anchor": 100}
    finally:
        await ctx.close()


async def test_runner_resource_unavailable_skips_retry(tmp_path: Path):
    """uid-level endpoint: ResourceUnavailableError -> FAILED_PERMANENT, no retries."""
    ctx, store = await _open_store(tmp_path, 701)
    try:
        attempts = [0]

        async def fake_fetch(uid, spec, credential, request_params, **kw):
            attempts[0] += 1
            raise ResourceUnavailableError("subscribed_bangumi code=53013: privacy")

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        result = await runner.run_task(701, endpoints=["subscribed_bangumi"])

        # Only one call — runner did NOT consume the retry budget.
        assert attempts[0] == 1
        assert result.endpoints["subscribed_bangumi"] == EndpointStatus.FAILED_PERMANENT
        assert result.status == TaskStatus.FAILED_PERMANENT
    finally:
        await ctx.close()


async def test_item_fanout_resource_unavailable_only_skips_one_item(tmp_path: Path):
    """item-level fan-out: ResourceUnavailableError skips one item, others succeed."""
    ctx, store = await _open_store(tmp_path, 702)
    try:
        await store.init_task(["videos", "video_detail"])
        await store.save_raw_payload("videos", "", {
            "pages": [
                {"list": {"vlist": [{"bvid": "BV_ok"}, {"bvid": "BV_dead"}]}},
            ],
        })
        await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
        await store.update_endpoint_state(
            "video_detail", status=EndpointStatus.PENDING.value,
        )

        call_log: list[str] = []

        async def fake_item(item_id, credential, **kw):
            call_log.append(item_id)
            if item_id == "BV_dead":
                raise ResourceUnavailableError(
                    f"video_detail[{item_id}]: code=53013",
                )
            return {"info": {"bvid": item_id}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        runner = _runner(
            store,
            _settings(tmp_path),
            reporter=_reporter(ctx, 702, mode="full", endpoints=["video_detail"]),
        )
        with patch.object(spec, "callable", fake_item):
            await runner._run_item_endpoint(702, spec, credential=None, mode="full")

        # BV_dead failed exactly once (no retries); BV_ok succeeded.
        assert call_log.count("BV_dead") == 1
        assert call_log.count("BV_ok") == 1

        # video_detail status -> SUCCESS: terminally unavailable items are skipped.
        assert (
            await store.get_endpoint_status("video_detail")
            == EndpointStatus.SUCCESS.value
        )

        # The successful item is stored.
        ok = await store.get_raw_payload("video_detail", "BV_ok")
        assert ok is not None
        # Failed item is NOT stored.
        dead = await store.get_raw_payload("video_detail", "BV_dead")
        assert dead is None

        # Error record carries retryable=False.
        errs = await store.list_errors(endpoint="video_detail")
        dead_errs = [e for e in errs if (e.get("detail") or {}).get("item_id") == "BV_dead"]
        assert len(dead_errs) == 1
        assert dead_errs[0]["retryable"] is False

        events = await _list_stage_events(ctx)
        event_names = [event["event"] for event in events]
        assert event_names[0] == "fetch.endpoint.started"
        assert event_names[-1] == "fetch.endpoint.completed"
        assert sorted(event_names[1:-1]) == [
            "fetch.item.saved",
            "fetch.item.unavailable",
        ]
        unavailable = next(e for e in events if e["event"] == "fetch.item.unavailable")
        assert unavailable["level"] == "WARNING"
        assert unavailable["endpoint"] == "video_detail"
        assert unavailable["item_id"] == "BV_dead"
        saved = next(e for e in events if e["event"] == "fetch.item.saved")
        assert saved["item_id"] == "BV_ok"
        completed = events[-1]
        assert completed["data"]["status"] == "SUCCESS"
        assert completed["data"]["completed"] == 2
        assert completed["data"]["failed"] == 0
        assert completed["data"]["total"] == 2
        assert completed["data"]["skipped_unavailable"] == 1
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["item_progress"]["skipped_unavailable"] == 1
        assert state["item_progress"]["failed"] == 0
    finally:
        await ctx.close()


async def test_article_detail_fanout_filters_note_opus_style_items(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 703)
    try:
        await store.init_task(["articles", "article_detail"])
        await store.save_raw_payload("articles", "", {
            "pages": [
                {
                    "articles": [
                        {
                            "id": 50612667,
                            "template_id": 4,
                            "origin_template_id": 5,
                            "type": 2,
                            "category": {"id": 42, "name": "全部笔记"},
                        },
                        {
                            "id": 100,
                            "template_id": 1,
                            "origin_template_id": 1,
                            "type": 0,
                            "category": {"id": 1, "name": "旧专栏"},
                        },
                    ],
                },
            ],
        })
        await store.update_endpoint_state("articles", status=EndpointStatus.SUCCESS.value)
        await store.update_endpoint_state(
            "article_detail", status=EndpointStatus.PENDING.value,
        )

        call_log: list[str] = []

        async def fake_item(item_id, credential, **kw):
            call_log.append(item_id)
            return {
                "info": {"id": int(item_id)},
                "markdown": f"# {item_id}",
                "content_json": [],
            }

        spec = get_endpoint("article_detail")
        assert spec is not None
        runner = _runner(
            store,
            _settings(tmp_path),
            reporter=_reporter(ctx, 703, mode="full", endpoints=["article_detail"]),
        )
        with patch.object(spec, "callable", fake_item):
            await runner._run_item_endpoint(703, spec, credential=None, mode="full")

        assert call_log == ["100"]
        assert await store.get_raw_payload("article_detail", "100") is not None
        assert await store.get_raw_payload("article_detail", "50612667") is None

        state = await store.get_endpoint_state("article_detail")
        assert state is not None
        assert state["status"] == EndpointStatus.SUCCESS.value
        assert state["item_progress"] == {
            "total": 2,
            "completed": 2,
            "failed": 0,
            "skipped": 1,
            "skipped_existing": 0,
            "skipped_fresh": 0,
            "skipped_unavailable": 0,
            "skipped_filtered": 1,
        }

        events = await _list_stage_events(ctx)
        completed = events[-1]
        assert completed["event"] == "fetch.endpoint.completed"
        assert completed["data"]["total"] == 2
        assert completed["data"]["completed"] == 2
        assert completed["data"]["skipped_filtered"] == 1
    finally:
        await ctx.close()


async def test_item_fanout_incremental_skips_unavailable_items(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 7021)
    try:
        await store.init_task(["videos", "video_detail"])
        await store.save_raw_payload("videos", "", {
            "pages": [
                {"list": {"vlist": [{"bvid": "BV_ok"}, {"bvid": "BV_dead"}]}},
            ],
        })
        await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
        await store.update_endpoint_state(
            "video_detail", status=EndpointStatus.PENDING.value,
        )

        first_calls: list[str] = []

        async def first_fetch(item_id, credential, **kw):
            first_calls.append(item_id)
            if item_id == "BV_dead":
                raise ResourceUnavailableError(f"gone {item_id}")
            return {"info": {"bvid": item_id}, "tags": []}

        spec = get_endpoint("video_detail")
        assert spec is not None
        runner = _runner(store, _settings(tmp_path))
        with patch.object(spec, "callable", first_fetch):
            await runner._run_item_endpoint(7021, spec, credential=None, mode="full")

        assert set(first_calls) == {"BV_ok", "BV_dead"}
        assert await store.list_unavailable_items("video_detail") == ["BV_dead"]

        second_fetch = AsyncMock(return_value={"info": {"unexpected": True}})
        await store.update_endpoint_state(
            "video_detail", status=EndpointStatus.PENDING.value,
        )
        with patch.object(spec, "callable", second_fetch):
            await runner._run_item_endpoint(
                7021, spec, credential=None, mode="incremental",
            )

        assert second_fetch.await_count == 0
        state = await store.get_endpoint_state("video_detail")
        assert state is not None
        assert state["status"] == EndpointStatus.SUCCESS.value
        assert state["item_progress"]["skipped_existing"] == 1
        assert state["item_progress"]["skipped_unavailable"] == 1
    finally:
        await ctx.close()


async def test_parallel_item_phase_suppresses_nested_progress(tmp_path: Path):
    """The outer item progress bar owns terminal output during parallel fanout."""
    ctx, store = await _open_store(tmp_path, 703)
    try:
        await store.init_task(["videos", "video_detail"])
        await store.save_raw_payload("videos", "", {
            "pages": [{"list": {"vlist": [{"bvid": "BV1"}]}}],
        })
        await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
        await store.update_endpoint_state(
            "video_detail", status=EndpointStatus.PENDING.value,
        )

        runner = _runner(store, _settings(tmp_path))
        calls: list[dict] = []

        async def fake_run_item_endpoint(uid, spec, credential, mode, *, show_progress=True):
            calls.append({
                "uid": uid,
                "endpoint": spec.name,
                "mode": mode,
                "show_progress": show_progress,
            })
            await store.update_endpoint_state(
                spec.name,
                status=EndpointStatus.SUCCESS.value,
                item_progress={"total": 1, "completed": 1, "failed": 0},
            )

        with patch.object(
            runner, "_run_item_endpoint", new=fake_run_item_endpoint,
        ):
            result = await runner.run_or_resume(
                703, endpoints=["video_detail"], mode="full",
            )

        assert result.status == TaskStatus.SUCCESS
        assert calls == [{
            "uid": 703,
            "endpoint": "video_detail",
            "mode": "full",
            "show_progress": False,
        }]
    finally:
        await ctx.close()


async def test_runner_records_fetch_run_observability(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 11)
    try:
        runner = _runner(
            store,
            _settings(tmp_path),
            fetch_fn=AsyncMock(return_value=_fake_page(11, {"ok": True})),
            reporter=_reporter(
                ctx,
                11,
                mode="incremental",
                endpoints=["user_info"],
            ),
        )
        result = await runner.run_task(11, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS

        runs = await _list_stage_runs(ctx)
        assert len(runs) == 1
        assert runs[0]["command"] == "fetch"
        assert runs[0]["status"] == "SUCCESS"
        assert runs[0]["args"] == {
            "mode": "incremental",
            "endpoints": ["user_info"],
        }
        assert runs[0]["summary"]["status"] == "SUCCESS"
        assert runs[0]["summary"]["endpoints"] == {"user_info": "SUCCESS"}

        events = await _list_stage_events(ctx)
        assert [event["event"] for event in events] == [
            "fetch.run.started",
            "fetch.endpoint.started",
            "fetch.endpoint.completed",
            "fetch.run.completed",
        ]
        completed = events[2]
        assert completed["endpoint"] == "user_info"
        assert completed["data"]["status"] == "SUCCESS"
    finally:
        await ctx.close()


async def test_runner_uses_injected_progress_factory(tmp_path: Path):
    ctx, store = await _open_store(tmp_path, 12)
    progress_instances: list[_NullProgress] = []

    def progress_factory(*, total: int, label: str, **kwargs) -> _NullProgress:
        progress = _NullProgress(total=total, label=label, **kwargs)
        progress_instances.append(progress)
        return progress

    try:
        runner = _runner(
            store,
            _settings(tmp_path),
            fetch_fn=AsyncMock(return_value=_fake_page(12, {"ok": True})),
            progress_factory=progress_factory,
        )
        result = await runner.run_task(12, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS

        assert len(progress_instances) == 1
        progress = progress_instances[0]
        assert progress.total == 1
        assert progress.label == "fetch uid=12 endpoints"
        assert progress.updates == [(1, None)]
        assert progress.closed is True
    finally:
        await ctx.close()


async def test_runner_mixed_public_private_auth_failure_keeps_public_endpoint(
    tmp_path: Path,
):
    ctx, store = await _open_store(tmp_path, 3004)
    try:
        async def fake_fetch(uid, spec, credential, request_params, **kw):
            assert spec.name == "user_info"
            assert credential is None
            return _fake_page(uid, {"ok": True}, endpoint=spec.name)

        runner = _runner(
            store,
            _settings(tmp_path),
            fetch_fn=AsyncMock(side_effect=fake_fetch),
        )
        with patch(
            "bili_unit.fetching.runner.get_credential",
            new=AsyncMock(side_effect=AuthError("no sessdata")),
        ):
            result = await runner.run_task(
                3004, endpoints=["user_info", "user_medal"],
            )
        assert result.status == TaskStatus.PARTIAL
        assert result.endpoints["user_info"] == EndpointStatus.SUCCESS
        assert result.endpoints["user_medal"] == EndpointStatus.FAILED_PERMANENT
    finally:
        await ctx.close()


# ======================================================================
# Stale RUNNING takeover (issue #3)
# ======================================================================

async def test_running_task_within_threshold_is_rejected(tmp_path: Path):
    """Fresh RUNNING task (recent updated_at) -> returns RUNNING, no work done."""
    ctx, store = await _open_store(tmp_path, 9001)
    try:
        await store.init_task(["user_info"])
        await store.update_endpoint_state(
            "user_info", status=EndpointStatus.RUNNING.value,
        )
        # Mark task RUNNING — store stamps updated_at_ms with now, so it's "fresh".
        await store.update_task_status(TaskStatus.RUNNING.value)

        runner = _runner(
            store, _settings(tmp_path),
            fetch_fn=AsyncMock(return_value=_fake_page(9001, {"ok": True})),
            stale_running_threshold_ms=15 * 60 * 1000,
        )
        result = await runner.run_or_resume(9001, endpoints=["user_info"])

        assert result.status == TaskStatus.RUNNING
        # Endpoint not touched — still RUNNING in store.
        assert (
            await store.get_endpoint_status("user_info")
            == EndpointStatus.RUNNING.value
        )
    finally:
        await ctx.close()


async def test_running_task_past_threshold_is_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Stale RUNNING task -> resumes; final status reflects actual run."""
    import time as _time

    ctx, store = await _open_store(tmp_path, 9002)
    try:
        await store.init_task(["user_info"])
        await store.update_endpoint_state(
            "user_info", status=EndpointStatus.PENDING.value,
        )
        await store.update_task_status(TaskStatus.RUNNING.value)

        # Push the runner's clock 30 min ahead so the task looks stale.
        future_time = _time.time() + 30 * 60
        monkeypatch.setattr(
            "bili_unit.fetching.runner.time.time", lambda: future_time,
        )

        async def fake_fetch(uid_arg, spec, credential, request_params, **kw):
            return _fake_page(uid_arg, {"name": "ok"})

        runner = _runner(
            store, _settings(tmp_path), fetch_fn=fake_fetch,
            stale_running_threshold_ms=15 * 60 * 1000,
        )
        result = await runner.run_or_resume(9002, endpoints=["user_info"])

        assert result.status != TaskStatus.RUNNING
        assert (
            await store.get_endpoint_status("user_info")
            == EndpointStatus.SUCCESS.value
        )
    finally:
        await ctx.close()


async def test_running_task_with_missing_updated_at_is_treated_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Defensive: tv.updated_at == None -> age_ms huge -> stale -> takeover.

    The SQLite store always stamps updated_at_ms on every write, so we
    advance the runner's clock far into the future to simulate an old row.
    """
    import time as _time

    ctx, store = await _open_store(tmp_path, 9004)
    try:
        await store.init_task(["user_info"])
        await store.update_endpoint_state(
            "user_info", status=EndpointStatus.PENDING.value,
        )
        await store.update_task_status(TaskStatus.RUNNING.value)

        future_time = _time.time() + 30 * 60
        monkeypatch.setattr(
            "bili_unit.fetching.runner.time.time", lambda: future_time,
        )

        async def fake_fetch(uid_arg, spec, credential, request_params, **kw):
            return _fake_page(uid_arg, {"name": "ok"})

        runner = _runner(
            store, _settings(tmp_path), fetch_fn=fake_fetch,
            stale_running_threshold_ms=15 * 60 * 1000,
        )
        result = await runner.run_or_resume(9004, endpoints=["user_info"])
        assert result.status != TaskStatus.RUNNING
    finally:
        await ctx.close()


async def test_resume_with_explicit_endpoints_does_not_resume_old_endpoint_set(
    tmp_path: Path,
):
    """A partial old all-endpoint task must not override a new subset run."""
    ctx, store = await _open_store(tmp_path, 9005)
    try:
        await store.init_task(["videos"])
        await store.update_endpoint_state(
            "videos", status=EndpointStatus.FAILED_EXHAUSTED.value,
        )
        await store.update_task_status(TaskStatus.PARTIAL.value)

        seen: list[str] = []

        async def fake_fetch(uid_arg, spec, credential, request_params, **kw):
            seen.append(spec.name)
            return _fake_page(uid_arg, {"name": "ok"}, endpoint=spec.name)

        runner = _runner(store, _settings(tmp_path), fetch_fn=fake_fetch)
        result = await runner.run_or_resume(9005, endpoints=["user_info"])

        assert result.endpoints["user_info"] == EndpointStatus.SUCCESS
        assert "videos" not in result.endpoints
        assert seen == ["user_info"]
        payload_json = await ctx.conn.fetch_value(
            "SELECT payload FROM stage_task WHERE stage = 'fetching'",
        )
        assert json.loads(payload_json)["endpoints"] == ["user_info"]
    finally:
        await ctx.close()
