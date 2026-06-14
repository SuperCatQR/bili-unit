# tests for bili_unit/fetching/query
# Run: uv run pytest bili_unit/tests/test_query.py -v

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._env import BiliSettings
from bili_unit.fetching import (
    EndpointStatus,
    Http412Error,
)
from bili_unit.fetching.command import Command
from bili_unit.fetching.query import Query

from .conftest import _fake_page

# ======================================================================
# query — structural invariants
# ======================================================================

@pytest.mark.asyncio
async def test_query_no_store_key_leak(stores, rl_ctl, query: Query):
    """Query must return DTOs, never expose store keys."""
    ds, es = stores
    cmd = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=AsyncMock(return_value=_fake_page(30, {"ok": True})))
    await cmd.fetch_uid(30, endpoints=["user_info"])

    task = await query.get_task(30)
    assert task is not None
    # DTO should not contain internal store metadata
    assert not hasattr(task, "_key")
    assert not hasattr(task, "_store")

    ep = await query.get_endpoint(30, "user_info")
    assert ep is not None
    assert ep.available
    assert ep.raw_payload == {"ok": True}


@pytest.mark.asyncio
async def test_query_available_only_on_success(stores, rl_ctl, query: Query):
    """available=False when endpoint hasn't succeeded."""
    ds, es = stores

    async def fake_fetch(uid, spec, credential, request_params, **kw):
        raise Http412Error("412")

    cmd = Command(ds, es, rl_ctl, BiliSettings(), fetch_fn=AsyncMock(side_effect=fake_fetch))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        await cmd.fetch_uid(40, endpoints=["user_info"])

    ep = await query.get_endpoint(40, "user_info")
    assert ep is not None
    assert not ep.available
    assert ep.status != EndpointStatus.SUCCESS
