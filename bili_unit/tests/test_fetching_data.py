# tests for bili_unit/fetching/data
# Run: uv run pytest bili_unit/tests/test_data.py -v

from pathlib import Path

import pytest

from bili_unit.fetching.data import DataStore


def _strip(v: dict) -> dict:
    """Return a copy without the auto-injected updated_at field."""
    return {k: val for k, val in v.items() if k != "updated_at"}


@pytest.mark.asyncio
async def test_data_store_crud(tmp_path: Path):
    ds = DataStore(str(tmp_path / "test_data"))
    await ds.open()
    try:
        # put + get
        await ds.put("k1", {"a": 1})
        v = await ds.get("k1")
        assert _strip(v) == {"a": 1}
        assert "updated_at" in v

        # overwrite
        await ds.put("k1", {"a": 2})
        v = await ds.get("k1")
        assert _strip(v) == {"a": 2}

        # missing
        assert await ds.get("k2") is None

        # delete
        await ds.delete("k1")
        assert await ds.get("k1") is None

        # list_prefix
        await ds.put("uid:1:fetch:a", {"x": 1})
        await ds.put("uid:1:fetch:b", {"x": 2})
        await ds.put("uid:2:fetch:a", {"x": 3})
        items = await ds.list_prefix("uid:1")
        assert len(items) == 2
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_data_store_transactional_write(tmp_path: Path):
    ds = DataStore(str(tmp_path / "test_data"))
    await ds.open()
    try:
        await ds.write_fetch_page_and_progress(
            "uid:1:fetch:v", {"page": 1}, "uid:1:progress:v", {"done": False}
        )
        assert _strip(await ds.get("uid:1:fetch:v")) == {"page": 1}
        assert _strip(await ds.get("uid:1:progress:v")) == {"done": False}
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_data_store_delete_by_prefix(tmp_path: Path):
    """Delete all keys for a uid using list_prefix + delete (the pattern used by --delete-uid)."""
    ds = DataStore(str(tmp_path / "test_data"))
    await ds.open()
    try:
        await ds.put("uid:100:task", {"status": "SUCCESS"})
        await ds.put("uid:100:fetch:videos", {"data": "v"})
        await ds.put("uid:100:fetch:video_detail", {"data": "vd"})
        await ds.put("uid:100:fetch:video_detail:BV001", {"data": "item"})
        await ds.put("uid:100:progress:videos", {"done": True})
        await ds.put("uid:200:task", {"status": "SUCCESS"})
        await ds.put("uid:200:fetch:videos", {"data": "v2"})

        # Delete all keys for uid:100
        rows = await ds.list_prefix("uid:100:")
        count = 0
        for key, _ in rows:
            await ds.delete(key)
            count += 1
        assert count == 5

        # uid:100 data should be gone
        assert await ds.get("uid:100:task") is None
        assert await ds.get("uid:100:fetch:videos") is None
        remaining = await ds.list_prefix("uid:100:")
        assert len(remaining) == 0

        # uid:200 data should remain
        assert await ds.get("uid:200:task") is not None
        assert await ds.get("uid:200:fetch:videos") is not None
    finally:
        await ds.close()
