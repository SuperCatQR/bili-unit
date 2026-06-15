# tests for bili_unit.fetching.command (Phase 6 rewrite for SQLite stack).
#
# The Phase 3 Command opens its own UidContext per fetch_uid() call. These
# tests inject a fake fetch_fn so we never hit the network and verify the
# command's idempotency / mode behaviour through the new FetchingStore reads.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import (
    Http412Error,
    TaskStatus,
)
from bili_unit.fetching._bilibili_adapter import FetchPageResult
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.command import Command
from bili_unit.fetching.rate_limit import RateLimitController


def _settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(bili_db_dir=str(tmp_path))


def _rate_limit() -> RateLimitController:
    # Very high QPS + zero pause keeps the test loop tight.
    return RateLimitController(
        global_qps=1000.0,
        endpoint_qps=1000.0,
        video_detail_qps=1000.0,
        pause_seconds=0,
    )


def _fake_page(uid: int, payload: dict, *, endpoint: str = "user_info") -> FetchPageResult:
    return FetchPageResult(
        uid=uid, endpoint=endpoint, raw_payload=payload,
        is_last_page=True, next_request=None,
    )


async def _read_task_status(tmp_path: Path, uid: int) -> str | None:
    """Open a fresh UidContext just to read back the persisted task status."""
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    try:
        return await FetchingStore(ctx).get_task_status()
    finally:
        await ctx.close()


# ======================================================================
# command — idempotency
# ======================================================================

async def test_command_new_uid_creates_task(tmp_path: Path):
    settings = _settings(tmp_path)
    fetch_mock = AsyncMock(return_value=_fake_page(10, {"ok": True}))
    cmd = Command(settings, _rate_limit(), fetch_fn=fetch_mock)

    result = await cmd.fetch_uid(10, endpoints=["user_info"])
    assert result.status == TaskStatus.SUCCESS

    persisted = await _read_task_status(tmp_path, 10)
    assert persisted == TaskStatus.SUCCESS.value


async def test_command_success_incremental_reruns(tmp_path: Path):
    """Default mode (incremental) re-runs SUCCESS endpoints (does not skip)."""
    settings = _settings(tmp_path)
    fetch_mock = AsyncMock(return_value=_fake_page(11, {"ok": True}))
    cmd = Command(settings, _rate_limit(), fetch_fn=fetch_mock)

    r1 = await cmd.fetch_uid(11, endpoints=["user_info"])
    assert r1.status == TaskStatus.SUCCESS

    r2 = await cmd.fetch_uid(11, endpoints=["user_info"])
    assert r2.status == TaskStatus.SUCCESS

    # The fetch fn must have been invoked on BOTH runs.
    assert fetch_mock.await_count >= 2


async def test_command_partial_resumes(tmp_path: Path):
    """First run partially fails on videos; second run resumes and succeeds."""
    settings = _settings(tmp_path)

    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True}, endpoint="user_info")
        raise Http412Error("412")

    cmd1 = Command(
        settings, _rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch_1),
    )
    r1 = await cmd1.fetch_uid(20, endpoints=["user_info", "videos"])
    assert r1.status in (TaskStatus.PARTIAL, TaskStatus.FAILED_EXHAUSTED)

    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True}, endpoint="user_info")
        if spec.name == "videos":
            return FetchPageResult(
                uid=uid, endpoint="videos",
                raw_payload={"list": {"vlist": []}, "page": {"count": 0}},
                is_last_page=True, next_request=None,
            )
        raise RuntimeError(f"unexpected {spec.name}")

    cmd2 = Command(
        settings, _rate_limit(),
        fetch_fn=AsyncMock(side_effect=fake_fetch_2),
    )
    r2 = await cmd2.fetch_uid(20, endpoints=["user_info", "videos"])

    assert r2.status == TaskStatus.SUCCESS


async def test_command_close_is_noop(tmp_path: Path):
    """Phase 3 contract: Command.close() is a no-op (no per-stage stores)."""
    cmd = Command(_settings(tmp_path), _rate_limit())
    # Calling twice must not raise.
    await cmd.close()
    await cmd.close()
