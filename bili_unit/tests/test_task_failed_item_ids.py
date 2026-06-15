# Tests for the W3.2 ``failed_item_ids`` task field.
#
# Each stage's TaskValue / TaskDTO now carries a ``failed_item_ids`` list that
# answers "what work failed?" without forcing the consumer to join task.json
# with the error log. Encoding:
#
#   * fetching:   ``"endpoint"`` (uid-level) or ``"endpoint:item_id"`` (fan-out)
#   * parsing:    bare model name (no ErrorStore — model-level granularity)
#   * processing: ``"pipeline:item_type:item_id"``
#
# These tests construct store state directly + call the read-side query, which
# is enough to verify the join logic without rebuilding the full pipelines.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from bili_unit.fetching import (
    EndpointStatus,
    Http412Error,
    ResourceUnavailableError,
    TaskStatus,
)
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.keys import _task_key as _fetch_task_key
from bili_unit.fetching.query import Query as FetchingQuery
from bili_unit.fetching.task import EndpointEntry, TaskValue
from bili_unit.parsing import (
    ParsingModelStatus,
    ParsingTaskStatus,
)
from bili_unit.parsing.command import ParsingCommand
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _task_key as _parsing_task_key
from bili_unit.parsing.query import ParsingQuery
from bili_unit.processing import (
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _task_key as _proc_task_key
from bili_unit.processing.query import ProcessingQuery
from bili_unit.processing.task import PipelineEntry, ProcessingTaskValue

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def fetching_stores(tmp_path):
    fd = FetchingDataStore(str(tmp_path / "fetch-data"))
    fe = FetchingErrorStore(str(tmp_path / "fetch-error"))
    await fd.open()
    await fe.open()
    yield fd, fe
    await fd.close()
    await fe.close()


@pytest.mark.asyncio
async def test_fetching_dto_failed_item_ids_includes_item_level(fetching_stores):
    """video_detail item-level FAILED → DTO carries ``video_detail:BV1abc``."""
    fd, fe = fetching_stores
    uid = 1001
    bvid = "BV1abc"

    # Record an item-level failure: detail.item_id is the bvid, status reflects
    # PARTIAL_ITEM (some succeeded, this one didn't).
    await fe.record(
        Http412Error("rate limited"),
        uid=uid, endpoint="video_detail", retryable=False,
        detail={"item_id": bvid},
    )

    tv = TaskValue(
        uid=uid,
        status=TaskStatus.PARTIAL,
        endpoints={
            "user_info": EndpointEntry(status=EndpointStatus.SUCCESS),
            "video_detail": EndpointEntry(
                status=EndpointStatus.PARTIAL_ITEM,
                item_progress={"total": 2, "completed": 1, "failed": 1},
            ),
        },
        created_at=0,
        updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())

    qry = FetchingQuery(fd, fe)
    dto = await qry.get_task(uid)

    assert dto is not None
    assert dto.failed_item_ids == [f"video_detail:{bvid}"]


@pytest.mark.asyncio
async def test_fetching_dto_failed_item_ids_drops_retry_to_success(fetching_stores):
    """Regression: an item-level fetch that failed earlier but later
    succeeded gets a SUCCESS data-store record. The historical error
    record must NOT cause it to surface in ``failed_item_ids``."""
    fd, fe = fetching_stores
    uid = 1010
    bvid_ok = "BV1retry"
    bvid_bad = "BV1bad"

    # ok one: error happened then a SUCCESS record was written.
    await fe.record(
        Http412Error("rate limited (since cleared)"),
        uid=uid, endpoint="video_detail", retryable=True,
        detail={"item_id": bvid_ok},
    )
    await fd.put(f"uid:{uid}:fetch:video_detail:{bvid_ok}", {
        "uid": uid, "endpoint": "video_detail", "item_id": bvid_ok,
        "status": "SUCCESS", "raw_payload": {"info": {"bvid": bvid_ok}},
    })
    # bad one: error only, no SUCCESS record.
    await fe.record(
        Http412Error("still failing"),
        uid=uid, endpoint="video_detail", retryable=False,
        detail={"item_id": bvid_bad},
    )

    tv = TaskValue(
        uid=uid,
        status=TaskStatus.PARTIAL,
        endpoints={
            "video_detail": EndpointEntry(
                status=EndpointStatus.PARTIAL_ITEM,
                item_progress={"total": 2, "completed": 1, "failed": 1},
            ),
        },
        created_at=0, updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())

    qry = FetchingQuery(fd, fe)
    dto = await qry.get_task(uid)
    assert dto is not None
    # Only the still-failing item appears; the retry-to-success one is gone.
    assert dto.failed_item_ids == [f"video_detail:{bvid_bad}"]


@pytest.mark.asyncio
async def test_fetching_dto_failed_item_ids_uid_level_endpoint(fetching_stores):
    """user_info uid-level FAILED → DTO carries ``user_info`` (no item_id)."""
    fd, fe = fetching_stores
    uid = 1002

    # uid-level endpoint: error has no ``item_id`` in detail.
    err_id = await fe.record(
        ResourceUnavailableError("privacy: 53013"),
        uid=uid, endpoint="user_info", retryable=False,
    )
    tv = TaskValue(
        uid=uid,
        status=TaskStatus.PARTIAL,
        endpoints={
            "user_info": EndpointEntry(
                status=EndpointStatus.FAILED_PERMANENT,
                last_error_id=err_id,
            ),
            "videos": EndpointEntry(status=EndpointStatus.SUCCESS),
        },
        created_at=0,
        updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())

    qry = FetchingQuery(fd, fe)
    dto = await qry.get_task(uid)

    assert dto is not None
    assert "user_info" in dto.failed_item_ids
    # Bare endpoint name, NOT ``user_info:something``.
    assert all(":" not in entry or entry != "user_info"
               for entry in dto.failed_item_ids)


@pytest.mark.asyncio
async def test_fetching_runner_persists_failed_item_ids(
    fetching_stores, rl_ctl, settings,
):
    """End-of-run finalisation writes ``failed_item_ids`` into task.json so
    consumers reading task.json directly (without going through Query) can
    still see what failed."""
    from unittest.mock import AsyncMock as _AsyncMock

    from bili_unit.fetching.runner import Runner

    from .conftest import _fake_page

    fd, fe = fetching_stores
    uid = 1003

    async def fake_fetch(uid_, spec, credential, request_params, **kw):
        if spec.name == "user_info":
            return _fake_page(uid_, {"ok": True})
        if spec.name == "videos":
            raise Http412Error("412 videos")

    runner = Runner(
        fd, fe, rl_ctl, settings,
        fetch_fn=_AsyncMock(side_effect=fake_fetch),
    )
    await runner.run_task(uid, endpoints=["user_info", "videos"])

    # Assert task.json on disk carries failed_item_ids = ["videos"] (uid-level
    # endpoint failure → bare endpoint name).
    raw = await fd.get(_fetch_task_key(uid))
    assert raw is not None
    assert "failed_item_ids" in raw
    assert "videos" in raw["failed_item_ids"]

    # And the Query DTO reads the persisted list (not falling back to derive).
    qry = FetchingQuery(fd, fe)
    dto = await qry.get_task(uid)
    assert dto is not None
    assert "videos" in dto.failed_item_ids


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def parsing_store(tmp_path):
    pd = ParsingDataStore(str(tmp_path / "parse-data"))
    await pd.open()
    yield pd
    await pd.close()


@pytest.mark.asyncio
async def test_parsing_dto_failed_item_ids_records_failed_models(parsing_store):
    """A model that raises during parse_uid → its name lands in
    ``failed_item_ids`` on the DTO and is persisted to task.json."""
    pd = parsing_store
    uid = 2001

    fetch_query = MagicMock()
    cmd = ParsingCommand(pd, fetch_query)

    async def fake_parse_model(uid_, model_name, mode):
        if model_name == "article_post":
            raise RuntimeError("simulated parse failure")
        return 1

    with patch.object(cmd, "_parse_model", side_effect=fake_parse_model):
        result = await cmd.parse_uid(uid=uid, mode="full")

    assert result.status == ParsingTaskStatus.PARTIAL

    qry = ParsingQuery(pd)
    dto = await qry.get_task(uid)

    assert dto is not None
    assert dto.failed_item_ids == ["article_post"]
    # And confirm the same data is persisted (not just derived on the fly).
    raw = await pd.get(_parsing_task_key(uid))
    assert raw is not None
    assert raw["failed_item_ids"] == ["article_post"]
    # Sanity: that model's status is FAILED.
    assert raw["models"]["article_post"]["status"] == ParsingModelStatus.FAILED.value


@pytest.mark.asyncio
async def test_parsing_dto_failed_item_ids_empty_when_all_pass(parsing_store):
    """No failures → empty list rather than absent / None."""
    pd = parsing_store
    uid = 2002

    fetch_query = MagicMock()
    cmd = ParsingCommand(pd, fetch_query)

    async def fake_parse_model(uid_, model_name, mode):
        return 1

    with patch.object(cmd, "_parse_model", side_effect=fake_parse_model):
        await cmd.parse_uid(uid=uid, mode="full")

    qry = ParsingQuery(pd)
    dto = await qry.get_task(uid)
    assert dto is not None
    assert dto.failed_item_ids == []


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def processing_stores(tmp_path):
    pd = ProcessingDataStore(str(tmp_path / "proc-data"))
    pe = ProcessingErrorStore(str(tmp_path / "proc-error"))
    await pd.open()
    await pe.open()
    yield pd, pe
    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_processing_dto_failed_item_ids_uses_data_store(processing_stores):
    """A failed audio bvid (FAILED status in data store + matching error
    record) surfaces as ``audio:transcription:BV1abc`` in the DTO."""
    pd, pe = processing_stores
    uid = 3001
    bvid = "BV1abc"

    # Seed a task value with the audio pipeline + item_progress rollup that
    # claims one failed item (matching the error store record).
    tv = ProcessingTaskValue(
        uid=uid,
        status=ProcessingTaskStatus.PARTIAL,
        pipelines={
            "audio": PipelineEntry(
                status=ProcessingPipelineStatus.PARTIAL,
                items={"transcription": {
                    "total": 1, "completed": 0, "failed": 1, "skipped": 0,
                }},
            ),
        },
        created_at=0,
        updated_at=0,
    )
    await pd.put(_proc_task_key(uid), tv.to_dict())

    # Production runner writes both a data-store FAILED record AND an error
    # record on terminal failure; mirror that so the DTO can reconcile.
    await pd.put(f"uid:{uid}:proc:audio:{bvid}", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": bvid, "status": "FAILED",
    })
    await pe.record(
        RuntimeError("download bombed"),
        uid=uid, retryable=False,
        pipeline="audio", item_type="transcription", item_id=bvid,
    )

    qry = ProcessingQuery(data=pd, error=pe)
    dto = await qry.get_task(uid)
    assert dto is not None
    assert dto.failed_item_ids == [f"audio:transcription:{bvid}"]


@pytest.mark.asyncio
async def test_processing_dto_failed_item_ids_drops_retry_to_success(processing_stores):
    """Regression: an item that failed earlier but later succeeded is
    SUCCESS in the data store. Even though the error store still carries
    the historical retry-attempt records, the DTO must NOT list it."""
    pd, pe = processing_stores
    uid = 3010
    bvid = "BV1retry"

    tv = ProcessingTaskValue(
        uid=uid,
        status=ProcessingTaskStatus.SUCCESS,
        pipelines={
            "audio": PipelineEntry(
                status=ProcessingPipelineStatus.SUCCESS,
                items={"transcription": {
                    "total": 1, "completed": 1, "failed": 0, "skipped": 0,
                }},
            ),
        },
        created_at=0, updated_at=0,
    )
    await pd.put(_proc_task_key(uid), tv.to_dict())

    # Two retryable error records persist (forensic log) — the actual
    # current state is SUCCESS in the data store after the 3rd attempt.
    await pe.record(
        RuntimeError("ASR 401 attempt 1"),
        uid=uid, retryable=True,
        pipeline="audio", item_type="transcription", item_id=bvid,
    )
    await pe.record(
        RuntimeError("ASR 401 attempt 2"),
        uid=uid, retryable=True,
        pipeline="audio", item_type="transcription", item_id=bvid,
    )
    await pd.put(f"uid:{uid}:proc:audio:{bvid}", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": bvid, "status": "SUCCESS",
        "result": {"bvid": bvid, "pages": []},
    })

    qry = ProcessingQuery(data=pd, error=pe)
    dto = await qry.get_task(uid)
    assert dto is not None
    assert dto.failed_item_ids == []


@pytest.mark.asyncio
async def test_processing_dto_failed_item_ids_persisted_via_runner(processing_stores):
    """``ProcessingRunner._collect_failed_item_ids`` aggregates from the
    data store (current truth) so a retry-to-success on a later run
    drops out automatically."""
    from bili_unit._env import BiliSettings
    from bili_unit.processing.runner import ProcessingRunner

    pd, pe = processing_stores
    uid = 3002

    # Two FAILED items in data store — these are what currently show as
    # failed. Error store still has matching records (and one extra
    # uid-level record with no item_id, which must not surface).
    await pd.put(f"uid:{uid}:proc:audio:BVa", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": "BVa", "status": "FAILED",
    })
    await pd.put(f"uid:{uid}:proc:audio:BVb", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": "BVb", "status": "FAILED",
    })
    await pe.record(
        RuntimeError("err A"), uid=uid, retryable=False,
        pipeline="audio", item_type="transcription", item_id="BVa",
    )
    await pe.record(
        RuntimeError("err B"), uid=uid, retryable=True,
        pipeline="audio", item_type="transcription", item_id="BVb",
    )
    await pe.record(
        RuntimeError("uid-level config"), uid=uid, retryable=False,
        pipeline="audio",
    )

    s = BiliSettings(
        bili_processing_data_dir=str(pd.base) if hasattr(pd, "base") else "",
        bili_processing_error_dir=str(pe._base),
        bili_processing_temp_dir="/tmp/proc",
    )
    runner = ProcessingRunner(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=MagicMock(), settings=s,
    )

    ids = await runner._collect_failed_item_ids(uid)
    assert ids == ["audio:transcription:BVa", "audio:transcription:BVb"]


@pytest.mark.asyncio
async def test_processing_runner_collect_drops_retry_to_success(processing_stores):
    """Runner-side regression: same as the DTO test but exercising
    :meth:`ProcessingRunner._collect_failed_item_ids` directly. A
    SUCCESS-now item with historical error records is NOT collected."""
    from bili_unit._env import BiliSettings
    from bili_unit.processing.runner import ProcessingRunner

    pd, pe = processing_stores
    uid = 3011
    bvid_ok = "BV1ok"
    bvid_bad = "BV1bad"

    # ok one: now SUCCESS in data store, but errors remain.
    await pd.put(f"uid:{uid}:proc:audio:{bvid_ok}", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": bvid_ok, "status": "SUCCESS",
    })
    await pe.record(
        RuntimeError("transient 401"), uid=uid, retryable=True,
        pipeline="audio", item_type="transcription", item_id=bvid_ok,
    )
    # bad one: still FAILED in data store + error.
    await pd.put(f"uid:{uid}:proc:audio:{bvid_bad}", {
        "uid": uid, "pipeline": "audio", "item_type": "transcription",
        "item_id": bvid_bad, "status": "FAILED",
    })
    await pe.record(
        RuntimeError("permanent"), uid=uid, retryable=False,
        pipeline="audio", item_type="transcription", item_id=bvid_bad,
    )

    s = BiliSettings(
        bili_processing_data_dir=str(pd.base) if hasattr(pd, "base") else "",
        bili_processing_error_dir=str(pe._base),
        bili_processing_temp_dir="/tmp/proc",
    )
    runner = ProcessingRunner(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=MagicMock(), settings=s,
    )

    ids = await runner._collect_failed_item_ids(uid)
    assert ids == [f"audio:transcription:{bvid_bad}"]
