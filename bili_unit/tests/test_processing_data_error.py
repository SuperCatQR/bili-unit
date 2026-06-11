# tests for bili_unit/processing/data.py + error.py.

import pytest
import pytest_asyncio

from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore


@pytest_asyncio.fixture
async def proc_data(tmp_path):
    s = ProcessingDataStore(str(tmp_path / "proc-data"))
    await s.open()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def proc_error(tmp_path):
    s = ProcessingErrorStore(str(tmp_path / "proc-error"))
    await s.open()
    yield s
    await s.close()


# ---------- data store ---------------------------------------------------

@pytest.mark.asyncio
async def test_data_put_get_delete(proc_data):
    key = "uid:42:proc:video_metadata:BV1aa"
    value = {"uid": 42, "result": {"title": "T"}}
    await proc_data.put(key, value)
    got = await proc_data.get(key)
    assert got is not None
    assert got["uid"] == 42
    assert got["result"]["title"] == "T"
    assert "updated_at" in got

    await proc_data.delete(key)
    assert await proc_data.get(key) is None


@pytest.mark.asyncio
async def test_data_list_prefix(proc_data):
    await proc_data.put("uid:1:proc:video_metadata:BVa", {"a": 1})
    await proc_data.put("uid:1:proc:video_metadata:BVb", {"a": 2})
    await proc_data.put("uid:1:proc:articles:5", {"a": 3})
    await proc_data.put("uid:2:proc:video_metadata:BVc", {"a": 4})

    rows = await proc_data.list_prefix("uid:1:proc:video_metadata:")
    keys = sorted(k for k, _ in rows)
    assert keys == [
        "uid:1:proc:video_metadata:BVa",
        "uid:1:proc:video_metadata:BVb",
    ]

    rows = await proc_data.list_prefix("uid:1:proc:")
    assert len(rows) == 3

    rows = await proc_data.list_prefix("uid:1:")
    assert len(rows) == 3  # no task yet

    rows = await proc_data.list_prefix("uid:")
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_data_progress_keys(proc_data):
    # pipeline-level progress
    k1 = "uid:9:progress:audio"
    await proc_data.put(k1, {"done": False})
    assert (await proc_data.get(k1))["done"] is False

    # pipeline + item_type progress
    k2 = "uid:9:progress:transform:video_metadata"
    await proc_data.put(k2, {"total_items": 10})
    assert (await proc_data.get(k2))["total_items"] == 10


@pytest.mark.asyncio
async def test_data_update_task_pipeline(proc_data):
    key = "uid:7:task"
    await proc_data.put(key, {
        "uid": 7, "status": "PENDING", "pipelines": {}, "created_at": 0,
    })
    await proc_data.update_task_pipeline(
        key, "transform", "RUNNING",
        items={"video_metadata": {"total": 5, "completed": 2, "failed": 0, "skipped": 0}},
    )
    val = await proc_data.get(key)
    assert val["pipelines"]["transform"]["status"] == "RUNNING"
    assert val["pipelines"]["transform"]["items"]["video_metadata"]["completed"] == 2

    # update without items: status only
    await proc_data.update_task_pipeline(key, "transform", "SUCCESS")
    val = await proc_data.get(key)
    assert val["pipelines"]["transform"]["status"] == "SUCCESS"
    # items preserved
    assert val["pipelines"]["transform"]["items"]["video_metadata"]["completed"] == 2


# ---------- error store -------------------------------------------------

@pytest.mark.asyncio
async def test_error_record_and_list(proc_error):
    e1 = await proc_error.record(
        ValueError("boom"),
        uid=42,
        pipeline="transform",
        item_type="video_metadata",
        item_id="BVa",
        retryable="false",
    )
    e2 = await proc_error.record(
        RuntimeError("partial"),
        uid=42,
        pipeline="audio",
        item_type="transcription",
        item_id="BVb",
        retryable="true",
        detail={"step": "download"},
    )
    assert e1 != e2

    errs = await proc_error.list_errors(uid=42)
    assert len(errs) == 2
    types = {e.error_type for e in errs}
    assert types == {"ValueError", "RuntimeError"}

    by_uid = await proc_error.list_by_uid(42)
    assert len(by_uid) == 2

    all_errs = await proc_error.list_errors()
    assert len(all_errs) == 2

    # delete
    n = await proc_error.delete_by_uid(42)
    assert n == 2
    assert await proc_error.list_errors(uid=42) == []
