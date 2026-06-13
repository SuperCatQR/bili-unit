# tests for bili_unit.parsing.command and bili_unit.parsing.query
# Run: uv run pytest bili_unit/tests/test_parsing_command.py -v

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from bili_unit.parsing import (
    ParsingCommandResult,
    ParsingModelStatus,
    ParsingTaskStatus,
    ParsingTaskValue,
)
from bili_unit.parsing.command import ParsingCommand
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key, _task_key
from bili_unit.parsing.query import ParsingQuery

# ======================================================================
# Fixtures
# ======================================================================

@pytest_asyncio.fixture
async def parsing_store(tmp_path):
    store = ParsingDataStore(tmp_path / "parsing")
    await store.open()
    yield store
    await store.close()


@pytest.fixture
def fetch_query():
    """Mock fetching query -- parsing only reads from it via model parsers."""
    return MagicMock()


@pytest.fixture
def parsing_command(parsing_store, fetch_query):
    return ParsingCommand(parsing_store, fetch_query)


@pytest.fixture
def parsing_query(parsing_store):
    return ParsingQuery(parsing_store)


# ======================================================================
# ParsingCommand -- parse_uid
# ======================================================================

@pytest.mark.asyncio
async def test_parse_uid_creates_task(parsing_command, parsing_store):
    """All models succeed with positive counts -> overall SUCCESS."""

    async def fake_parse_model(uid, model_name, mode):
        # Return a positive count for every model
        return {"user_profile": 1, "video_detail": 5, "article": 3, "opus": 2, "dynamic": 4}[
            model_name
        ]

    with patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model):
        result = await parsing_command.parse_uid(uid=1001, mode="full")

    assert isinstance(result, ParsingCommandResult)
    assert result.uid == 1001
    assert result.status == ParsingTaskStatus.SUCCESS

    # Verify the task was persisted with correct model entries
    task_d = await parsing_store.get(_task_key(1001))
    assert task_d is not None
    assert task_d["status"] == ParsingTaskStatus.SUCCESS.value

    models = task_d["models"]
    assert set(models.keys()) == {"user_profile", "video_detail", "article", "opus", "dynamic"}

    for model_name in ("user_profile", "video_detail", "article", "opus", "dynamic"):
        assert models[model_name]["status"] == ParsingModelStatus.SUCCESS.value
        assert models[model_name]["count"] > 0


@pytest.mark.asyncio
async def test_parse_uid_partial_status(parsing_command, parsing_store):
    """Some models return 0 -> overall PARTIAL, zero-count models still SUCCESS."""

    async def fake_parse_model(uid, model_name, mode):
        # video_detail and opus return 0 (nothing to parse)
        return {"user_profile": 1, "video_detail": 0, "article": 2, "opus": 0, "dynamic": 3}[
            model_name
        ]

    with patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model):
        result = await parsing_command.parse_uid(uid=2002)

    assert result.status == ParsingTaskStatus.PARTIAL

    task_d = await parsing_store.get(_task_key(2002))
    assert task_d is not None
    assert task_d["status"] == ParsingTaskStatus.PARTIAL.value

    # Zero-count models are still marked SUCCESS (they ran fine, just found nothing new)
    assert task_d["models"]["video_detail"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task_d["models"]["video_detail"]["count"] == 0
    assert task_d["models"]["opus"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task_d["models"]["opus"]["count"] == 0

    # Positive-count models
    assert task_d["models"]["user_profile"]["count"] == 1
    assert task_d["models"]["article"]["count"] == 2


@pytest.mark.asyncio
async def test_parse_uid_model_failure(parsing_command, parsing_store):
    """One model raises -> that model is FAILED, overall PARTIAL, others unaffected."""

    async def fake_parse_model(uid, model_name, mode):
        if model_name == "article":
            raise RuntimeError("simulated parse failure")
        return {"user_profile": 1, "video_detail": 2, "opus": 1, "dynamic": 1}[model_name]

    with patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model):
        result = await parsing_command.parse_uid(uid=3003)

    assert result.status == ParsingTaskStatus.PARTIAL

    task_d = await parsing_store.get(_task_key(3003))
    assert task_d is not None

    # The failed model
    assert task_d["models"]["article"]["status"] == ParsingModelStatus.FAILED.value

    # The other models should still be SUCCESS
    assert task_d["models"]["user_profile"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task_d["models"]["video_detail"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task_d["models"]["opus"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task_d["models"]["dynamic"]["status"] == ParsingModelStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_parse_uid_with_download_images(parsing_command):
    """download_images=True triggers _download_images after model parsing."""

    async def fake_parse_model(uid, model_name, mode):
        return 1

    fake_images_summary = {"total": 3, "ok": 2, "skipped": 1, "failed": 0, "failed_urls": []}

    with (
        patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model),
        patch.object(
            parsing_command, "_download_images", new=AsyncMock(return_value=fake_images_summary),
        ) as mock_dl,
    ):
        result = await parsing_command.parse_uid(uid=4004, download_images=True)

    assert result.status == ParsingTaskStatus.SUCCESS
    mock_dl.assert_awaited_once_with(4004)


@pytest.mark.asyncio
async def test_parse_uid_without_download_images(parsing_command):
    """download_images=False (default) must NOT call _download_images."""

    async def fake_parse_model(uid, model_name, mode):
        return 1

    with (
        patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model),
        patch.object(
            parsing_command, "_download_images", new=AsyncMock(),
        ) as mock_dl,
    ):
        await parsing_command.parse_uid(uid=5005, download_images=False)

    mock_dl.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_uid_download_images_failure_does_not_affect_status(parsing_command):
    """If _download_images raises, overall status is still based on model parsing."""

    async def fake_parse_model(uid, model_name, mode):
        return 1

    with (
        patch.object(parsing_command, "_parse_model", side_effect=fake_parse_model),
        patch.object(
            parsing_command, "_download_images", new=AsyncMock(side_effect=RuntimeError("dl fail")),
        ),
    ):
        result = await parsing_command.parse_uid(uid=6006, download_images=True)

    # All models succeeded with count > 0, so status is SUCCESS despite image failure
    assert result.status == ParsingTaskStatus.SUCCESS


# ======================================================================
# ParsingQuery -- task accessors
# ======================================================================

@pytest.mark.asyncio
async def test_query_get_task_none(parsing_query):
    """No task in store -> returns None."""
    result = await parsing_query.get_task(uid=9999)
    assert result is None


@pytest.mark.asyncio
async def test_query_get_task_success(parsing_store, parsing_query):
    """Put a task dict directly into the store, verify query returns correct DTO."""
    uid = 7007
    tv = ParsingTaskValue(
        uid=uid,
        status=ParsingTaskStatus.SUCCESS,
        models={
            "user_profile": {"status": "SUCCESS", "count": 1},
            "video_detail": {"status": "SUCCESS", "count": 10},
            "article": {"status": "FAILED", "count": 0},
        },
        created_at=1700000000000,
        updated_at=1700000001000,
    )
    await parsing_store.put(_task_key(uid), tv.to_dict())

    dto = await parsing_query.get_task(uid)
    assert dto is not None
    assert dto.uid == uid
    assert dto.status == ParsingTaskStatus.SUCCESS
    assert dto.created_at == 1700000000000
    assert dto.updated_at is not None  # auto-injected by store

    # Model DTOs
    assert "user_profile" in dto.models
    assert dto.models["user_profile"].status == ParsingModelStatus.SUCCESS
    assert dto.models["user_profile"].count == 1

    assert dto.models["video_detail"].count == 10

    assert dto.models["article"].status == ParsingModelStatus.FAILED
    assert dto.models["article"].count == 0


@pytest.mark.asyncio
async def test_query_list_tasks(parsing_store, parsing_query):
    """Put multiple task dicts, verify list_tasks returns all sorted by uid."""
    for uid in (100, 200, 300):
        tv = ParsingTaskValue(
            uid=uid,
            status=ParsingTaskStatus.SUCCESS,
            models={"user_profile": {"status": "SUCCESS", "count": 1}},
            created_at=1700000000000,
        )
        await parsing_store.put(_task_key(uid), tv.to_dict())

    tasks = await parsing_query.list_tasks()
    assert len(tasks) == 3
    assert [t["uid"] for t in tasks] == [100, 200, 300]
    assert all(t["status"] == ParsingTaskStatus.SUCCESS for t in tasks)
    assert all(t["model_count"] == 1 for t in tasks)


@pytest.mark.asyncio
async def test_query_list_tasks_empty(parsing_query):
    """Empty store -> empty list."""
    tasks = await parsing_query.list_tasks()
    assert tasks == []


# ======================================================================
# ParsingQuery -- typed object accessors
# ======================================================================

@pytest.mark.asyncio
async def test_query_get_user_profile(parsing_store, parsing_query):
    """Put a user_profile dict, verify query returns it."""
    uid = 8008
    profile = {"uid": uid, "name": "test_user", "face": "https://example.com/face.jpg"}
    await parsing_store.put(_item_key(uid, "user_profile", str(uid)), profile)

    result = await parsing_query.get_user_profile(uid)
    assert result is not None
    assert result["uid"] == uid
    assert result["name"] == "test_user"


@pytest.mark.asyncio
async def test_query_get_user_profile_none(parsing_query):
    """No profile stored -> returns None."""
    result = await parsing_query.get_user_profile(uid=9999)
    assert result is None


@pytest.mark.asyncio
async def test_query_list_video_details(parsing_store, parsing_query):
    """Put multiple video_detail dicts, verify list returns them all."""
    uid = 9009
    for bvid in ("BV1xx", "BV2yy", "BV3zz"):
        detail = {"bvid": bvid, "title": f"Video {bvid}", "uid": uid}
        await parsing_store.put(_item_key(uid, "video_detail", bvid), detail)

    results = await parsing_query.list_video_details(uid)
    assert len(results) == 3
    bvids = {r["bvid"] for r in results}
    assert bvids == {"BV1xx", "BV2yy", "BV3zz"}


@pytest.mark.asyncio
async def test_query_list_video_details_empty(parsing_query):
    """No videos stored -> empty list."""
    results = await parsing_query.list_video_details(uid=9999)
    assert results == []


@pytest.mark.asyncio
async def test_query_get_video_detail(parsing_store, parsing_query):
    """Put a single video_detail dict, verify get returns it."""
    uid = 1010
    bvid = "BV1abc"
    detail = {"bvid": bvid, "title": "Test Video", "duration": 120}
    await parsing_store.put(_item_key(uid, "video_detail", bvid), detail)

    result = await parsing_query.get_video_detail(uid, bvid)
    assert result is not None
    assert result["bvid"] == bvid
    assert result["title"] == "Test Video"


@pytest.mark.asyncio
async def test_query_get_video_detail_none(parsing_query):
    """No matching video -> returns None."""
    result = await parsing_query.get_video_detail(uid=9999, bvid="BVnone")
    assert result is None


@pytest.mark.asyncio
async def test_query_list_articles(parsing_store, parsing_query):
    uid = 1111
    for cvid in ("cv100", "cv200"):
        article = {"cvid": cvid, "title": f"Article {cvid}"}
        await parsing_store.put(_item_key(uid, "article", cvid), article)

    results = await parsing_query.list_articles(uid)
    assert len(results) == 2
    assert {r["cvid"] for r in results} == {"cv100", "cv200"}


@pytest.mark.asyncio
async def test_query_get_article(parsing_store, parsing_query):
    uid = 1212
    cvid = "cv999"
    article = {"cvid": cvid, "title": "Deep Dive"}
    await parsing_store.put(_item_key(uid, "article", cvid), article)

    result = await parsing_query.get_article(uid, cvid)
    assert result is not None
    assert result["cvid"] == cvid
    assert result["title"] == "Deep Dive"


@pytest.mark.asyncio
async def test_query_list_opus(parsing_store, parsing_query):
    uid = 1313
    for opus_id in ("op1", "op2", "op3"):
        opus = {"opus_id": opus_id, "content": f"Opus {opus_id}"}
        await parsing_store.put(_item_key(uid, "opus", opus_id), opus)

    results = await parsing_query.list_opus(uid)
    assert len(results) == 3
    assert {r["opus_id"] for r in results} == {"op1", "op2", "op3"}


@pytest.mark.asyncio
async def test_query_get_opus(parsing_store, parsing_query):
    uid = 1414
    opus_id = "op42"
    opus = {"opus_id": opus_id, "content": "Hello opus"}
    await parsing_store.put(_item_key(uid, "opus", opus_id), opus)

    result = await parsing_query.get_opus(uid, opus_id)
    assert result is not None
    assert result["opus_id"] == opus_id
    assert result["content"] == "Hello opus"


@pytest.mark.asyncio
async def test_query_list_dynamics(parsing_store, parsing_query):
    uid = 1515
    for dyn_id in ("dyn10", "dyn20"):
        dynamic = {"dynamic_id": dyn_id, "text": f"Dynamic {dyn_id}"}
        await parsing_store.put(_item_key(uid, "dynamic", dyn_id), dynamic)

    results = await parsing_query.list_dynamics(uid)
    assert len(results) == 2
    assert {r["dynamic_id"] for r in results} == {"dyn10", "dyn20"}


@pytest.mark.asyncio
async def test_query_get_dynamic(parsing_store, parsing_query):
    uid = 1616
    dynamic_id = "dyn99"
    dynamic = {"dynamic_id": dynamic_id, "text": "Some dynamic"}
    await parsing_store.put(_item_key(uid, "dynamic", dynamic_id), dynamic)

    result = await parsing_query.get_dynamic(uid, dynamic_id)
    assert result is not None
    assert result["dynamic_id"] == dynamic_id
    assert result["text"] == "Some dynamic"


# ======================================================================
# ParsingQuery -- images DTO in task
# ======================================================================

@pytest.mark.asyncio
async def test_query_get_task_with_images(parsing_store, parsing_query):
    """Task with images block should populate the images DTO."""
    uid = 1717
    tv = ParsingTaskValue(
        uid=uid,
        status=ParsingTaskStatus.SUCCESS,
        models={"user_profile": {"status": "SUCCESS", "count": 1}},
        images={"total": 5, "ok": 3, "skipped": 1, "failed": 1, "failed_urls": ["https://bad"]},
        created_at=1700000000000,
    )
    await parsing_store.put(_task_key(uid), tv.to_dict())

    dto = await parsing_query.get_task(uid)
    assert dto is not None
    assert dto.images is not None
    assert dto.images.total == 5
    assert dto.images.ok == 3
    assert dto.images.skipped == 1
    assert dto.images.failed == 1
    assert dto.images.failed_urls == ["https://bad"]


@pytest.mark.asyncio
async def test_query_get_task_without_images(parsing_store, parsing_query):
    """Task without images block -> images DTO is None."""
    uid = 1818
    tv = ParsingTaskValue(
        uid=uid,
        status=ParsingTaskStatus.SUCCESS,
        models={"user_profile": {"status": "SUCCESS", "count": 1}},
        created_at=1700000000000,
    )
    await parsing_store.put(_task_key(uid), tv.to_dict())

    dto = await parsing_query.get_task(uid)
    assert dto is not None
    assert dto.images is None
