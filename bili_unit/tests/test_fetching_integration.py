# integration tests — full command → runner → query closed loop
# Run: uv run pytest bili_unit/tests/test_integration.py -v

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import TaskStatus
from bili_unit.fetching._bilibili_adapter import FetchPageResult
from bili_unit.fetching.command import Command
from bili_unit.fetching.query import Query

from .conftest import _fake_page, _fake_videos_pages

# ======================================================================
# full loop — single endpoint
# ======================================================================

@pytest.mark.asyncio
async def test_integration_single_endpoint_success(
    command: Command, query: Query,
):
    """Mock client → command.fetch_uid() → query.get_task() → verify."""
    user_info_data = {"code": 0, "data": {"mid": 123, "name": "test"}}

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(return_value=_fake_page(123, user_info_data)),
    ):
        result = await command.fetch_uid(123, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS

    task = await query.get_task(123)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS
    assert "user_info" in task.endpoints
    ep = task.endpoints["user_info"]
    assert ep.available
    assert ep.raw_payload == user_info_data


# ======================================================================
# full loop — multi-endpoint, multi-page videos
# ======================================================================

@pytest.mark.asyncio
async def test_integration_multi_endpoint(
    command: Command, query: Query,
):
    """user_info succeeds; videos succeeds with 3 pages."""
    pages = list(_fake_videos_pages(999, total_pages=3))

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid, {"name": "a"})
        if spec.name == "videos":
            pn = request_params.get("pn", 1)
            if pn <= len(pages):
                return pages[pn - 1]
            return FetchPageResult(uid=uid, endpoint="videos", raw_payload={"list": []}, is_last_page=True)
        raise RuntimeError(f"unexpected {spec.name}")

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        result = await command.fetch_uid(999, endpoints=["user_info", "videos"])
        assert result.status == TaskStatus.SUCCESS

    task = await query.get_task(999)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS
    assert task.endpoints["user_info"].available
    assert task.endpoints["videos"].available
    # Videos should have accumulated all 3 pages
    raw = task.endpoints["videos"].raw_payload
    assert raw is not None
    assert "pages" in raw
    assert len(raw["pages"]) == 3
    # First page has 30 items, last page has 1 item
    assert len(raw["pages"][0]["list"]["vlist"]) == 30
    assert len(raw["pages"][2]["list"]["vlist"]) == 1


# ======================================================================
# delete uid — full loop: fetch → verify → delete → verify gone
# ======================================================================

@pytest.mark.asyncio
async def test_integration_delete_uid(
    command: Command, query: Query, stores,
):
    """Fetch data for a uid, delete it, verify all data is gone."""
    ds, es = stores

    user_info_data = {"code": 0, "data": {"mid": 555, "name": "delme"}}

    with patch(
        "bili_unit.fetching.runner.fetch_endpoint",
        new=AsyncMock(return_value=_fake_page(555, user_info_data)),
    ):
        result = await command.fetch_uid(555, endpoints=["user_info"])
        assert result.status == TaskStatus.SUCCESS

    # Verify data exists
    task = await query.get_task(555)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS

    # Delete all data for uid=555
    all_rows = await ds.list_prefix("uid:555:")
    count = 0
    for key, _ in all_rows:
        await ds.delete(key)
        count += 1
    err_count = await es.delete_by_uid(555)

    assert count > 0  # Should have deleted at least task + fetch keys
    assert err_count == 0  # No errors were recorded in this test

    # Verify data is gone
    task_after = await query.get_task(555)
    assert task_after is None

    # Delete non-existent uid should be safe
    rows2 = await ds.list_prefix("uid:555:")
    assert len(rows2) == 0
