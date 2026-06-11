# tests for bili_unit/fetching/command
# Run: uv run pytest bili_unit/tests/test_command.py -v

from unittest.mock import AsyncMock, patch

import pytest

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
async def test_command_new_uid_creates_task(command: Command, query: Query):
    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(return_value=_fake_page(10, {"ok": True})),
    ):
        result = await command.fetch_uid(10, endpoints=["user_info"])
    assert result.status == TaskStatus.SUCCESS

    task = await query.get_task(10)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_command_success_incremental_reruns(command: Command):
    """Default mode (incremental) re-runs SUCCESS endpoints."""
    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(return_value=_fake_page(11, {"ok": True})),
    ):
        r1 = await command.fetch_uid(11, endpoints=["user_info"])
        assert r1.status == TaskStatus.SUCCESS

        # second call — incremental re-runs, not skips
        r2 = await command.fetch_uid(11, endpoints=["user_info"])
        assert r2.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_command_partial_resumes(stores, rl_ctl):
    ds, es = stores
    cmd = Command(ds, es, rl_ctl)

    # First run: user_info succeeds, videos gets 412
    async def fake_fetch_1(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True})
        raise Http412Error("412")

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(side_effect=fake_fetch_1),
    ), patch("bili_unit.fetching.runner.RETRY_DELAYS", [0, 0, 0]):
        r1 = await cmd.fetch_uid(20, endpoints=["user_info", "videos"])
    assert r1.status in (TaskStatus.PARTIAL, TaskStatus.FAILED_EXHAUSTED)

    # Second run (resume): videos now succeeds; user_info also re-runs (incremental reset)
    async def fake_fetch_2(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"ok": True})
        if spec.name == "videos":
            return _fake_page(uid, {"list": {"vlist": []}, "page": {"count": 0}})
        raise RuntimeError("unexpected")

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(side_effect=fake_fetch_2),
    ):
        r2 = await cmd.fetch_uid(20, endpoints=["user_info", "videos"])

    assert r2.status == TaskStatus.SUCCESS
