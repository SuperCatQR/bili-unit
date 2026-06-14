# tests for bili_unit/fetching/command
# Run: uv run pytest bili_unit/tests/test_command.py -v

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._env import BiliSettings
from bili_unit.fetching import (
    Http412Error,
    TaskStatus,
)
from bili_unit.fetching.command import Command
from bili_unit.fetching.query import Query

from .conftest import _fake_page

# ======================================================================
# command — idempotency
# ======================================================================

@pytest.mark.asyncio
async def test_command_new_uid_creates_task(stores, rl_ctl, query: Query):
    ds, es = stores
    fetch_mock = AsyncMock(return_value=_fake_page(10, {"ok": True}))
    cmd = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=fetch_mock)
    result = await cmd.fetch_uid(10, endpoints=["user_info"])
    assert result.status == TaskStatus.SUCCESS

    task = await query.get_task(10)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_command_success_incremental_reruns(stores, rl_ctl):
    """Default mode (incremental) re-runs SUCCESS endpoints."""
    ds, es = stores
    fetch_mock = AsyncMock(return_value=_fake_page(11, {"ok": True}))
    cmd = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=fetch_mock)

    r1 = await cmd.fetch_uid(11, endpoints=["user_info"])
    assert r1.status == TaskStatus.SUCCESS

    # second call — incremental re-runs, not skips
    r2 = await cmd.fetch_uid(11, endpoints=["user_info"])
    assert r2.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_command_partial_resumes(stores, rl_ctl):
    ds, es = stores

    # First run: user_info succeeds, videos gets 412
    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True})
        raise Http412Error("412")

    cmd = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=AsyncMock(side_effect=fake_fetch_1))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        r1 = await cmd.fetch_uid(20, endpoints=["user_info", "videos"])
    assert r1.status in (TaskStatus.PARTIAL, TaskStatus.FAILED_EXHAUSTED)

    # Second run (resume): videos now succeeds; user_info also re-runs (incremental reset)
    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True})
        if spec.name == "videos":
            return _fake_page(uid, {"list": {"vlist": []}, "page": {"count": 0}})
        raise RuntimeError("unexpected")

    cmd2 = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=AsyncMock(side_effect=fake_fetch_2))
    r2 = await cmd2.fetch_uid(20, endpoints=["user_info", "videos"])

    assert r2.status == TaskStatus.SUCCESS
