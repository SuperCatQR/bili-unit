# Integration tests for processing runner / command / query.
# Uses real fetching DataStore as upstream (populated with raw_payload),
# real fetching Query, real processing stores. No external API calls.

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.keys import (
    _fetch_key,
    _item_fetch_key,
)
from bili_unit.fetching.keys import (
    _task_key as _fetch_task_key,
)
from bili_unit.fetching.query import Query as FetchingQuery
from bili_unit.fetching.task import EndpointEntry, TaskValue
from bili_unit.processing import (
    AudioError,
    DownloadError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.env import ProcessingEnv
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _proc_key
from bili_unit.processing.query import ProcessingQuery
from bili_unit.processing.runner import ProcessingRunner


def _make_settings(tmp_path, max_retries=3, retry_delays="30,60,120") -> ProcessingEnv:
    """Fast settings: tiny worker pool, deterministic queue size."""
    return ProcessingEnv(
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_transform_workers=2,
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=max_retries,
        bili_processing_retry_delays=retry_delays,
    )


async def _fake_process_audio_one(runner, uid, item, credential):
    """Mock _process_audio_one: write a fake SUCCESS result to the data store."""
    bvid = item.item_id
    now = int(time.time() * 1000)
    await runner._data.put(_proc_key(uid, "audio", bvid), {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": ProcessingItemStatus.SUCCESS.value,
        "result": {
            "bvid": bvid,
            "pages": [
                {"page_index": p["page_index"], "cid": p["cid"],
                 "duration": 60.0, "text": f"mock transcription for {bvid}",
                 "language": "auto", "asr_model": "mock-asr-v0", "segments": []}
                for p in item.item_data.get("pages", [])
            ],
            "total_duration": 60.0,
            "total_chars": len(f"mock transcription for {bvid}"),
        },
        "source_endpoints": ["video_detail"],
        "processed_at": now,
    })
    return True


@pytest_asyncio.fixture
async def fetching_stack(tmp_path):
    fd = FetchingDataStore(str(tmp_path / "fetch-data"))
    fe = FetchingErrorStore(str(tmp_path / "fetch-error"))
    await fd.open()
    await fe.open()
    qry = FetchingQuery(fd, fe)
    yield fd, fe, qry
    await fd.close()
    await fe.close()


@pytest_asyncio.fixture
async def proc_stack(tmp_path, fetching_stack):
    fd, fe, fqry = fetching_stack
    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    # Mock audio processing + credential so existing transform tests are
    # unaffected by the audio pipeline running alongside.
    with (
        patch.object(
            ProcessingRunner, "_process_audio_one",
            new=_fake_process_audio_one,
        ),
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield cmd, qry, pd, pe, fd

    await pd.close()
    await pe.close()


async def _seed_fetching_video_detail(
    fd: FetchingDataStore, uid: int, bvids: list[str], success: bool = True,
) -> None:
    """Populate fetching's data store with a SUCCESS video_detail aggregation +
    per-bvid raw payloads — mimicking the layout that the runner expects."""
    # task value with video_detail SUCCESS
    tv = TaskValue(
        uid=uid,
        status=TaskStatus.SUCCESS,
        endpoints={
            "video_detail": EndpointEntry(
                status=EndpointStatus.SUCCESS,
                item_progress={
                    "total": len(bvids),
                    "completed": len(bvids) if success else 0,
                    "failed": 0 if success else len(bvids),
                },
            ),
        },
        created_at=0,
        updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())

    # aggregation row
    await fd.put(_fetch_key(uid, "video_detail"), {
        "uid": uid,
        "endpoint": "video_detail",
        "status": EndpointStatus.SUCCESS.value if success else EndpointStatus.FAILED_EXHAUSTED.value,
        "raw_payload": None,
        "item_counts": {
            "total": len(bvids),
            "completed": len(bvids) if success else 0,
            "failed": 0 if success else len(bvids),
        },
    })

    # per-bvid raw payloads
    for bvid in bvids:
        await fd.put(_item_fetch_key(uid, "video_detail", bvid), {
            "uid": uid,
            "endpoint": "video_detail",
            "item_id": bvid,
            "status": EndpointStatus.SUCCESS.value,
            "raw_payload": {
                "info": {
                    "bvid": bvid,
                    "aid": int(bvid[2:]) if bvid[2:].isdigit() else 0,
                    "title": f"title-{bvid}",
                    "desc": "",
                    "duration": 60,
                    "pages": [{"cid": 1, "part": "P1", "duration": 60}],
                    "stat": {"view": 1, "danmaku": 0, "reply": 0,
                             "favorite": 0, "coin": 0, "share": 0, "like": 1},
                    "owner": {"mid": 999, "name": "U"},
                },
                "tags": [{"tag_name": "t1"}],
            },
        })


async def _seed_fetching_endpoint(
    fd: FetchingDataStore, uid: int, endpoint: str, raw_payload: dict,
) -> None:
    # task with this endpoint SUCCESS
    existing = await fd.get(_fetch_task_key(uid))
    tv = TaskValue.from_dict(existing) if existing else TaskValue(uid=uid)
    tv.status = TaskStatus.SUCCESS
    tv.endpoints[endpoint] = EndpointEntry(status=EndpointStatus.SUCCESS)
    await fd.put(_fetch_task_key(uid), tv.to_dict())

    await fd.put(_fetch_key(uid, endpoint), {
        "uid": uid,
        "endpoint": endpoint,
        "status": EndpointStatus.SUCCESS.value,
        "raw_payload": raw_payload,
        "fetched_at": 0,
    })


# ---------- transform integration ---------------------------------------

@pytest.mark.asyncio
async def test_processing_video_metadata_happy_path(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 100
    bvids = ["BV001", "BV002", "BV003"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    result = await cmd.process_uid(uid, item_types=["video_metadata"], mode="incremental")
    assert result.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    assert task is not None
    pipe = task.pipelines["transform"]
    assert pipe.status == ProcessingPipelineStatus.SUCCESS
    counts = pipe.items["video_metadata"]
    assert counts["total"] == 3
    assert counts["completed"] == 3
    assert counts["failed"] == 0

    items = await qry.list_items(uid, "video_metadata")
    assert sorted(it.item_id for it in items) == ["BV001", "BV002", "BV003"]
    for it in items:
        assert it.status == ProcessingItemStatus.SUCCESS
        assert it.result["bvid"] == it.item_id
        assert it.result["title"] == f"title-{it.item_id}"


@pytest.mark.asyncio
async def test_processing_dynamics_and_articles(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 200

    await _seed_fetching_endpoint(fd, uid, "dynamics", {
        "pages": [
            {"items": [
                {"id_str": "DYN1", "type": "T",
                 "modules": [{"module_dynamic": {"desc": {"text": "hello"}}}]},
                {"id_str": "DYN2", "type": "T",
                 "modules": [{"module_dynamic": {"desc": {"text": "world"}}}]},
            ]},
        ],
    })
    await _seed_fetching_endpoint(fd, uid, "articles", {
        "pages": [
            {"articles": [
                {"id": 1, "title": "A1", "summary": "s1"},
                {"id": 2, "title": "A2", "summary": "s2"},
            ]},
        ],
    })

    result = await cmd.process_uid(uid, item_types=["dynamics", "articles"])
    assert result.status == ProcessingTaskStatus.SUCCESS

    dyns = await qry.list_items(uid, "dynamics")
    assert {it.item_id for it in dyns} == {"DYN1", "DYN2"}
    assert all(it.status == ProcessingItemStatus.SUCCESS for it in dyns)

    arts = await qry.list_items(uid, "articles")
    assert {it.item_id for it in arts} == {"1", "2"}


@pytest.mark.asyncio
async def test_processing_incremental_skip_existing(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 300
    bvids = ["BVa", "BVb"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    # First run
    r1 = await cmd.process_uid(uid, item_types=["video_metadata"])
    assert r1.status == ProcessingTaskStatus.SUCCESS
    items1 = await qry.list_items(uid, "video_metadata")
    ts1 = {it.item_id: it.processed_at for it in items1}

    # Second incremental run — should skip both
    r2 = await cmd.process_uid(uid, item_types=["video_metadata"], mode="incremental")
    assert r2.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    counts = task.pipelines["transform"].items["video_metadata"]
    # Both already-stored items count as skipped this run
    assert counts["skipped"] == 2
    assert counts["completed"] == 0
    assert counts["total"] == 2

    items2 = await qry.list_items(uid, "video_metadata")
    ts2 = {it.item_id: it.processed_at for it in items2}
    # processed_at unchanged because items were skipped
    assert ts1 == ts2


@pytest.mark.asyncio
async def test_processing_full_mode_overwrites(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 301
    bvids = ["BVx"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    r1 = await cmd.process_uid(uid, item_types=["video_metadata"])
    assert r1.status == ProcessingTaskStatus.SUCCESS
    items1 = await qry.list_items(uid, "video_metadata")
    ts1 = items1[0].processed_at

    # full re-run
    import asyncio
    await asyncio.sleep(0.005)  # bump epoch_ms so processed_at differs
    r2 = await cmd.process_uid(uid, item_types=["video_metadata"], mode="full")
    assert r2.status == ProcessingTaskStatus.SUCCESS
    items2 = await qry.list_items(uid, "video_metadata")
    counts = (await qry.get_task(uid)).pipelines["transform"].items["video_metadata"]
    assert counts["completed"] == 1  # re-processed
    assert counts["skipped"] == 0
    assert items2[0].processed_at >= ts1


@pytest.mark.asyncio
async def test_processing_partial_item_only_success(proc_stack, fetching_stack):
    """When fetching reports PARTIAL_ITEM, processing only handles SUCCESS items."""
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 400
    # Seed task: video_detail PARTIAL_ITEM, two items SUCCESS one FAILED.
    tv = TaskValue(
        uid=uid,
        status=TaskStatus.PARTIAL,
        endpoints={
            "video_detail": EndpointEntry(
                status=EndpointStatus.PARTIAL_ITEM,
                item_progress={"total": 3, "completed": 2, "failed": 1},
            ),
        },
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())
    await fd.put(_fetch_key(uid, "video_detail"), {
        "uid": uid,
        "endpoint": "video_detail",
        "status": EndpointStatus.PARTIAL_ITEM.value,
        "raw_payload": None,
        "item_counts": {"total": 3, "completed": 2, "failed": 1},
    })
    # ok items
    for bvid in ("BVok1", "BVok2"):
        await fd.put(_item_fetch_key(uid, "video_detail", bvid), {
            "uid": uid, "endpoint": "video_detail", "item_id": bvid,
            "status": EndpointStatus.SUCCESS.value,
            "raw_payload": {"info": {"bvid": bvid, "title": "t"}, "tags": []},
        })
    # failed item
    await fd.put(_item_fetch_key(uid, "video_detail", "BVbad"), {
        "uid": uid, "endpoint": "video_detail", "item_id": "BVbad",
        "status": EndpointStatus.FAILED_EXHAUSTED.value,
        "raw_payload": None,
    })

    r = await cmd.process_uid(uid, item_types=["video_metadata"])
    assert r.status == ProcessingTaskStatus.SUCCESS
    items = await qry.list_items(uid, "video_metadata")
    ids = {it.item_id for it in items}
    assert ids == {"BVok1", "BVok2"}


@pytest.mark.asyncio
async def test_processing_endpoint_unavailable_skips_handler(proc_stack):
    cmd, qry, _pd, _pe, _fd = proc_stack
    uid = 500
    # No fetching data seeded → endpoint unavailable → no items.
    r = await cmd.process_uid(uid, item_types=["video_metadata"])
    # No work items: pipeline becomes SUCCESS (empty rollup).
    assert r.status == ProcessingTaskStatus.SUCCESS
    assert await qry.list_items(uid, "video_metadata") == []


@pytest.mark.asyncio
async def test_processing_handler_failure_records_error(proc_stack, fetching_stack):
    """If transform raises, the item is FAILED and an error is recorded."""
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 600
    # Seed a video_detail with a payload that will explode the handler:
    # we patch the handler temporarily.
    await _seed_fetching_video_detail(fd, uid, ["BVcrash"])

    # Monkeypatch the handler.transform to raise once.
    from bili_unit.processing.transform import video_metadata as vm
    original = vm.HANDLER.transform

    def boom(_item):
        raise RuntimeError("kaboom")

    vm.HANDLER.transform = boom  # type: ignore[method-assign]
    try:
        r = await cmd.process_uid(uid, item_types=["video_metadata"])
    finally:
        vm.HANDLER.transform = original  # type: ignore[method-assign]

    # Pipeline ends FAILED_PERMANENT (1 failed, 0 completed).
    assert r.status == ProcessingTaskStatus.PARTIAL or \
           r.status == ProcessingTaskStatus.FAILED_PERMANENT
    items = await qry.list_items(uid, "video_metadata")
    assert len(items) == 1
    assert items[0].status == ProcessingItemStatus.FAILED
    errs = await qry.list_errors(uid=uid)
    assert any(e.error_type == "RuntimeError" for e in errs)


@pytest.mark.asyncio
async def test_processing_video_full_view(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 700
    bvids = ["BVfull"]
    await _seed_fetching_video_detail(fd, uid, bvids)
    await cmd.process_uid(uid, item_types=["video_metadata"])

    full = await qry.get_video_full(uid, "BVfull")
    assert full is not None
    assert full.metadata is not None
    assert full.metadata.result["title"] == "title-BVfull"
    # After running both pipelines with mocked audio, transcription is present.
    assert full.transcription is not None
    assert full.transcription.status == ProcessingItemStatus.SUCCESS

    summaries = await qry.list_all_videos(uid)
    assert len(summaries) == 1
    assert summaries[0].bvid == "BVfull"
    assert summaries[0].has_transcription is True


# ---------- audio pipeline integration ---------------------------------------

@pytest.mark.asyncio
async def test_audio_pipeline_discovers_and_processes(proc_stack, fetching_stack):
    """Audio pipeline discovers items from video_detail and processes them."""
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 800
    bvids = ["BVaud1", "BVaud2"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    result = await cmd.process_uid(uid, pipelines=["audio"], mode="incremental")
    assert result.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    assert "audio" in task.pipelines
    pipe = task.pipelines["audio"]
    assert pipe.status == ProcessingPipelineStatus.SUCCESS
    counts = pipe.items["transcription"]
    assert counts["total"] == 2
    assert counts["completed"] == 2
    assert counts["failed"] == 0

    # Verify audio results were stored
    audio_items = await qry.list_items(uid, "audio")
    assert {it.item_id for it in audio_items} == {"BVaud1", "BVaud2"}
    for it in audio_items:
        assert it.status == ProcessingItemStatus.SUCCESS
        assert it.result["bvid"] == it.item_id
        assert len(it.result["pages"]) >= 1


@pytest.mark.asyncio
async def test_audio_pipeline_incremental_skip(proc_stack, fetching_stack):
    """Audio incremental mode skips already-SUCCESS items."""
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 801
    await _seed_fetching_video_detail(fd, uid, ["BVskip"])

    # First run
    await cmd.process_uid(uid, pipelines=["audio"])
    items1 = await qry.list_items(uid, "audio")
    assert len(items1) == 1
    ts1 = items1[0].processed_at

    # Second incremental run
    r2 = await cmd.process_uid(uid, pipelines=["audio"], mode="incremental")
    assert r2.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    counts = task.pipelines["audio"].items["transcription"]
    assert counts["skipped"] == 1
    assert counts["completed"] == 0

    items2 = await qry.list_items(uid, "audio")
    assert items2[0].processed_at == ts1  # unchanged


@pytest.mark.asyncio
async def test_audio_pipeline_failure_records_error(tmp_path, fetching_stack):
    """When audio download fails, the item is FAILED and error recorded."""
    fd, fe, fqry = fetching_stack
    uid = 802
    await _seed_fetching_video_detail(fd, uid, ["BVfail"])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    # Mock the downloader to raise; real _process_audio_one handles it.
    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("audio boom"))

    with (
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
    ):
        result = await cmd.process_uid(uid, pipelines=["audio"])

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT

    audio_items = await qry.list_items(uid, "audio")
    assert len(audio_items) == 1
    assert audio_items[0].status == ProcessingItemStatus.FAILED

    errs = await qry.list_errors(uid=uid)
    assert any(e.error_type == "RuntimeError" for e in errs)

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_pipeline_no_video_detail(proc_stack):
    """Audio pipeline gracefully handles missing video_detail."""
    cmd, qry, _pd, _pe, _fd = proc_stack
    uid = 803
    result = await cmd.process_uid(uid, pipelines=["audio"])
    assert result.status == ProcessingTaskStatus.SUCCESS
    assert await qry.list_items(uid, "audio") == []


# ---------- retry behaviour -------------------------------------------------

@pytest.mark.asyncio
async def test_is_retryable_classification():
    """_is_retryable returns True for AudioError, False for everything else."""
    assert ProcessingRunner._is_retryable(DownloadError("cdn")) is True
    assert ProcessingRunner._is_retryable(AudioError("generic audio")) is True
    assert ProcessingRunner._is_retryable(RuntimeError("boom")) is False
    assert ProcessingRunner._is_retryable(ValueError("bad")) is False


@pytest.mark.asyncio
async def test_audio_retry_exhausts_then_fails(tmp_path, fetching_stack):
    """Retryable audio error retries max_retries times, then final FAILED."""
    fd, _fe, fqry = fetching_stack
    uid = 900
    await _seed_fetching_video_detail(fd, uid, ["BVretry"])

    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    # AudioDownloader always raises DownloadError (retryable).
    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=DownloadError("cdn down"))

    with (
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
        patch("bili_unit.processing.runner.asyncio.sleep", new=AsyncMock()),
    ):
        result = await cmd.process_uid(uid, pipelines=["audio"])

    # All retries exhausted → FAILED_PERMANENT (0 completed, 1 failed).
    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT

    items = await qry.list_items(uid, "audio")
    assert len(items) == 1
    assert items[0].status == ProcessingItemStatus.FAILED

    # Error store: 2 intermediate retries + 1 final = 3 records.
    errs = await qry.list_errors(uid=uid)
    audio_errs = [e for e in errs if e.error_type == "DownloadError"]
    assert len(audio_errs) == 3
    # Intermediate retries marked retryable=true, final marked retryable=false.
    assert audio_errs[0].retryable == "true"
    assert audio_errs[1].retryable == "true"
    assert audio_errs[2].retryable == "false"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_retry_succeeds_after_first_failure(tmp_path, fetching_stack):
    """Retryable error on attempt 0, success on attempt 1 → SUCCESS."""
    fd, _fe, fqry = fetching_stack
    uid = 901
    await _seed_fetching_video_detail(fd, uid, ["BVretryOk"])

    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    mock_dl = AsyncMock()
    # First call: retryable error; second call: success.
    mock_dl.get_audio_url = AsyncMock(
        side_effect=[DownloadError("transient"), {"url": "https://cdn/x", "duration": 60}],
    )
    mock_dl.download_to_file = AsyncMock()

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(return_value=type("R", (), {"text": "hi", "duration": 60})())
    mock_asr.model = "mock-asr"
    mock_asr.close = AsyncMock()

    with (
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
        patch(
            "bili_unit.processing.runner.convert_single",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
        patch("bili_unit.processing.runner.asyncio.sleep", new=AsyncMock()),
    ):
        result = await cmd.process_uid(uid, pipelines=["audio"])

    assert result.status == ProcessingTaskStatus.SUCCESS
    items = await qry.list_items(uid, "audio")
    assert len(items) == 1
    assert items[0].status == ProcessingItemStatus.SUCCESS

    # One intermediate error recorded (retryable=true).
    errs = await qry.list_errors(uid=uid)
    audio_errs = [e for e in errs if e.error_type == "DownloadError"]
    assert len(audio_errs) == 1
    assert audio_errs[0].retryable == "true"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_non_retryable_no_retry(tmp_path, fetching_stack):
    """Non-retryable error (RuntimeError) → no retry, immediate FAILED."""
    fd, _fe, fqry = fetching_stack
    uid = 902
    await _seed_fetching_video_detail(fd, uid, ["BVnoRetry"])

    s = _make_settings(tmp_path, max_retries=3, retry_delays="0,0,0")
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("not retryable"))

    with (
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
    ):
        result = await cmd.process_uid(uid, pipelines=["audio"])

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    # Only 1 error record (no retries).
    errs = await qry.list_errors(uid=uid)
    assert len(errs) == 1
    assert errs[0].retryable == "false"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_zero_max_retries_immediate_fail(tmp_path, fetching_stack):
    """max_retries=0 → one attempt, no retry on retryable error."""
    fd, _fe, fqry = fetching_stack
    uid = 903
    await _seed_fetching_video_detail(fd, uid, ["BVzero"])

    s = _make_settings(tmp_path, max_retries=0, retry_delays="0")
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry)

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=DownloadError("fail"))

    with (
        patch(
            "bili_unit.fetching.auth.get_credential",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
    ):
        result = await cmd.process_uid(uid, pipelines=["audio"])

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    errs = await qry.list_errors(uid=uid)
    assert len(errs) == 1
    assert errs[0].retryable == "false"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_duration_uses_page_metadata_not_last_segment(
    tmp_path, fetching_stack,
):
    """Multi-segment ASR: ``page_duration`` must reflect the page's true
    length, not just the last segment's duration.

    Regression for the bug where 1033 s clips landed in the store with
    ``duration=204`` because each loop iteration overwrote ``page_duration``
    with the last ASR segment's value.
    """
    from bili_unit.processing.audio import ASRResult
    from bili_unit.processing.transform._base import WorkItem

    fd, _fe, fqry = fetching_stack
    uid = 950
    bvid = "BVdurFix"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )

    # 1033s page, segmented into two ASR calls returning 830 + 204.
    work_item = WorkItem(
        item_type="audio",
        item_id=bvid,
        item_data={
            "bvid": bvid,
            "pages": [{"page_index": 0, "cid": 1, "duration": 1033, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        return_value={"url": "https://cdn/x", "duration": 999.0},
    )
    mock_dl.download_to_file = AsyncMock()

    # convert_single returns two fake mp3 paths.
    seg_files = [
        tmp_path / "seg_000.mp3",
        tmp_path / "seg_001.mp3",
    ]
    for f in seg_files:
        f.write_bytes(b"x")

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="part-A", duration=830.0, model="m"),
        ASRResult(text="part-B", duration=204.0, model="m"),
    ])
    mock_asr.model = "m"
    mock_asr.close = AsyncMock()

    with (
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
        patch(
            "bili_unit.processing.runner.convert_single",
            new=AsyncMock(return_value=seg_files),
        ),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert result["bvid"] == bvid
    assert len(result["pages"]) == 1
    page = result["pages"][0]
    # Duration must equal the page's *true* length (from metadata),
    # not just the last segment's 204.0.
    assert page["duration"] == 1033.0
    assert result["total_duration"] == 1033.0
    # Text must be the joined transcription of both segments.
    assert page["text"] == "part-A part-B"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_duration_falls_back_to_segment_sum_when_no_metadata(
    tmp_path, fetching_stack,
):
    """When page metadata has no duration (e.g. malformed data), sum the
    per-segment ASR durations instead of using only the last one."""
    from bili_unit.processing.audio import ASRResult
    from bili_unit.processing.transform._base import WorkItem

    fd, _fe, fqry = fetching_stack
    uid = 951
    bvid = "BVdurSum"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )

    work_item = WorkItem(
        item_type="audio",
        item_id=bvid,
        item_data={
            "bvid": bvid,
            # No duration field on the page.
            "pages": [{"page_index": 0, "cid": 1, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        return_value={"url": "https://cdn/x"},  # no duration either
    )
    mock_dl.download_to_file = AsyncMock()

    seg_files = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
    for f in seg_files:
        f.write_bytes(b"x")

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="x", duration=300.0, model="m"),
        ASRResult(text="y", duration=120.0, model="m"),
    ])
    mock_asr.model = "m"

    with (
        patch(
            "bili_unit.processing.runner.AudioDownloader",
            return_value=mock_dl,
        ),
        patch(
            "bili_unit.processing.runner.convert_single",
            new=AsyncMock(return_value=seg_files),
        ),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert result["pages"][0]["duration"] == 420.0  # 300 + 120, not 120
    assert result["total_duration"] == 420.0

    await pd.close()
    await pe.close()
