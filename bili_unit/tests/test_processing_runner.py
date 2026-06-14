# Integration tests for processing runner / command / query.
#
# After the processing-shrink refactor:
#   - Transform pipeline removed entirely.
#   - Audio pipeline reads from FetchingQuery (video_detail raw items).
#   - get_video_full / list_all_videos read metadata from ParsingQuery.
#
# Test fixtures:
#   fetching_stack — FetchingDataStore + Query (for audio pipeline)
#   parsing_stack  — ParsingDataStore + Query (for video_full / list_all_videos)
#   proc_stack     — full ProcessingCommand wired to both stacks

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
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key as _parsing_item_key
from bili_unit.parsing.query import ParsingQuery
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
from bili_unit.processing.runner._pipeline_executor import WorkItem


def _make_settings(tmp_path, max_retries=3, retry_delays="30,60,120") -> ProcessingEnv:
    """Fast settings: tiny worker pool, deterministic queue size."""
    return ProcessingEnv(
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=max_retries,
        bili_processing_retry_delays=retry_delays,
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
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


# -- fixtures ---------------------------------------------------------------


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
async def parsing_stack(tmp_path):
    pd = ParsingDataStore(str(tmp_path / "parse-data"))
    await pd.open()
    pq = ParsingQuery(data=pd)
    yield pd, pq
    await pd.close()


@pytest_asyncio.fixture
async def proc_stack(tmp_path, fetching_stack, parsing_stack):
    fd, fe, fqry = fetching_stack
    pd_parse, pqry = parsing_stack
    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )
    qry = ProcessingQuery(data=pd, error=pe, fetching_query=fqry, parsing_query=pqry)

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


# -- seeding helpers --------------------------------------------------------

async def _seed_fetching_video_detail(
    fd: FetchingDataStore, uid: int, bvids: list[str], success: bool = True,
) -> None:
    """Populate fetching store with SUCCESS video_detail data (for audio)."""
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
    await fd.put(_fetch_key(uid, "video_detail"), {
        "uid": uid, "endpoint": "video_detail",
        "status": EndpointStatus.SUCCESS.value if success else EndpointStatus.FAILED_EXHAUSTED.value,
        "raw_payload": None,
        "item_counts": {
            "total": len(bvids),
            "completed": len(bvids) if success else 0,
            "failed": 0 if success else len(bvids),
        },
    })
    for bvid in bvids:
        await fd.put(_item_fetch_key(uid, "video_detail", bvid), {
            "uid": uid, "endpoint": "video_detail", "item_id": bvid,
            "status": EndpointStatus.SUCCESS.value,
            "raw_payload": {
                "info": {
                    "bvid": bvid,
                    "aid": int(bvid[2:]) if bvid[2:].isdigit() else 0,
                    "title": f"title-{bvid}",
                    "desc": "", "duration": 60,
                    "pages": [{"cid": 1, "part": "P1", "duration": 60}],
                    "stat": {"view": 1, "danmaku": 0, "reply": 0,
                             "favorite": 0, "coin": 0, "share": 0, "like": 1},
                    "owner": {"mid": 999, "name": "U"},
                },
                "tags": [{"tag_name": "t1"}],
            },
        })


async def _seed_parsing_video_details(
    pd: ParsingDataStore, uid: int, bvids: list[str],
) -> None:
    """Write VideoDetail typed-object dicts to the parsing store."""
    for bvid in bvids:
        await pd.put(_parsing_item_key(uid, "video_detail", bvid), {
            "_model_name": "video_detail",
            "bvid": bvid,
            "aid": int(bvid[2:]) if bvid[2:].isdigit() else 0,
            "title": f"title-{bvid}",
            "desc": "", "duration": 60,
            "ctime": None, "pubdate": None, "pic": "",
            "pages": [{"cid": 1, "part": "P1", "duration": 60, "dimension": {}, "first_frame": ""}],
            "tags": ["t1"],
            "stat": {"view": 1, "danmaku": 0, "reply": 0, "favorite": 0, "coin": 0, "share": 0, "like": 1},
            "owner": {"mid": 999, "name": "U", "face": ""},
            "rights": {}, "subtitle": {}, "label": {},
            "pic_local": "",
        })


# ---------- video_full aggregate view -----------------------------------------

@pytest.mark.asyncio
async def test_processing_video_full_view(proc_stack, fetching_stack, parsing_stack):
    """get_video_full returns metadata from parsing + transcription from audio."""
    cmd, qry, _pd, _pe, fd = proc_stack
    pd_parse, _pq = parsing_stack
    uid = 700
    bvids = ["BVfull"]
    # Seed fetching (for audio pipeline) and parsing (for metadata).
    await _seed_fetching_video_detail(fd, uid, bvids)
    await _seed_parsing_video_details(pd_parse, uid, bvids)

    # Run audio pipeline so transcription record exists.
    await cmd.process_uid(uid)

    full = await qry.get_video_full(uid, "BVfull")
    assert full is not None
    assert full.metadata is not None
    # metadata.result is the parsing dict itself
    assert full.metadata.result["title"] == "title-BVfull"
    assert full.metadata.pipeline == "parsing"
    assert full.transcription is not None
    assert full.transcription.status == ProcessingItemStatus.SUCCESS

    summaries = await qry.list_all_videos(uid)
    assert len(summaries) == 1
    assert summaries[0].bvid == "BVfull"
    assert summaries[0].has_transcription is True
    assert summaries[0].title == "title-BVfull"


# ---------- audio pipeline integration ---------------------------------------

@pytest.mark.asyncio
async def test_audio_pipeline_discovers_and_processes(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 800
    bvids = ["BVaud1", "BVaud2"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    result = await cmd.process_uid(uid, mode="incremental")
    assert result.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    assert "audio" in task.pipelines
    pipe = task.pipelines["audio"]
    assert pipe.status == ProcessingPipelineStatus.SUCCESS
    counts = pipe.items["transcription"]
    assert counts["total"] == 2
    assert counts["completed"] == 2
    assert counts["failed"] == 0

    audio_items = await qry.list_items(uid, "audio")
    assert {it.item_id for it in audio_items} == {"BVaud1", "BVaud2"}
    for it in audio_items:
        assert it.status == ProcessingItemStatus.SUCCESS
        assert it.result["bvid"] == it.item_id
        assert len(it.result["pages"]) >= 1


@pytest.mark.asyncio
async def test_audio_pipeline_incremental_skip(proc_stack, fetching_stack):
    cmd, qry, _pd, _pe, fd = proc_stack
    uid = 801
    await _seed_fetching_video_detail(fd, uid, ["BVskip"])

    await cmd.process_uid(uid)
    items1 = await qry.list_items(uid, "audio")
    assert len(items1) == 1
    ts1 = items1[0].processed_at

    r2 = await cmd.process_uid(uid, mode="incremental")
    assert r2.status == ProcessingTaskStatus.SUCCESS

    task = await qry.get_task(uid)
    counts = task.pipelines["audio"].items["transcription"]
    assert counts["skipped"] == 1
    assert counts["completed"] == 0

    items2 = await qry.list_items(uid, "audio")
    assert items2[0].processed_at == ts1


@pytest.mark.asyncio
async def test_audio_pipeline_failure_records_error(tmp_path, fetching_stack):
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

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("audio boom"))

    with (
        patch("bili_unit.fetching.auth.get_credential", new=AsyncMock(return_value=None)),
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
    ):
        result = await cmd.process_uid(uid)

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
    cmd, qry, _pd, _pe, _fd = proc_stack
    uid = 803
    result = await cmd.process_uid(uid)
    assert result.status == ProcessingTaskStatus.SUCCESS
    assert await qry.list_items(uid, "audio") == []


# ---------- retry behaviour -------------------------------------------------

@pytest.mark.asyncio
async def test_is_retryable_classification():
    assert ProcessingRunner._is_retryable(DownloadError("cdn")) is True
    assert ProcessingRunner._is_retryable(AudioError("generic audio")) is True
    assert ProcessingRunner._is_retryable(RuntimeError("boom")) is False
    assert ProcessingRunner._is_retryable(ValueError("bad")) is False


@pytest.mark.asyncio
async def test_audio_retry_exhausts_then_fails(tmp_path, fetching_stack):
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

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=DownloadError("cdn down"))

    with (
        patch("bili_unit.fetching.auth.get_credential", new=AsyncMock(return_value=None)),
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.asyncio.sleep", new=AsyncMock()),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT

    items = await qry.list_items(uid, "audio")
    assert len(items) == 1
    assert items[0].status == ProcessingItemStatus.FAILED

    errs = await qry.list_errors(uid=uid)
    audio_errs = [e for e in errs if e.error_type == "DownloadError"]
    assert len(audio_errs) == 3
    assert audio_errs[0].retryable is True
    assert audio_errs[1].retryable is True
    assert audio_errs[2].retryable is False

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_retry_succeeds_after_first_failure(tmp_path, fetching_stack):
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
    mock_dl.get_audio_url = AsyncMock(
        side_effect=[DownloadError("transient"), {"url": "https://cdn/x", "duration": 60}],
    )
    mock_dl.download_to_file = AsyncMock()

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(return_value=type("R", (), {"text": "hi", "duration": 60})())
    mock_asr.model = "mock-asr"
    mock_asr.close = AsyncMock()

    with (
        patch("bili_unit.fetching.auth.get_credential", new=AsyncMock(return_value=None)),
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=[])),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
        patch("bili_unit.processing.runner.asyncio.sleep", new=AsyncMock()),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    items = await qry.list_items(uid, "audio")
    assert len(items) == 1
    assert items[0].status == ProcessingItemStatus.SUCCESS

    errs = await qry.list_errors(uid=uid)
    audio_errs = [e for e in errs if e.error_type == "DownloadError"]
    assert len(audio_errs) == 1
    assert audio_errs[0].retryable is True

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_non_retryable_no_retry(tmp_path, fetching_stack):
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
        patch("bili_unit.fetching.auth.get_credential", new=AsyncMock(return_value=None)),
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    errs = await qry.list_errors(uid=uid)
    assert len(errs) == 1
    assert errs[0].retryable is False

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_zero_max_retries_immediate_fail(tmp_path, fetching_stack):
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
        patch("bili_unit.fetching.auth.get_credential", new=AsyncMock(return_value=None)),
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    errs = await qry.list_errors(uid=uid)
    assert len(errs) == 1
    assert errs[0].retryable is False

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_duration_uses_page_metadata_not_last_segment(
    tmp_path, fetching_stack,
):
    from bili_unit.processing.audio import ASRResult

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

    work_item = WorkItem(
        item_type="audio",
        item_id=bvid,
        item_data={
            "bvid": bvid,
            "pages": [{"page_index": 0, "cid": 1, "duration": 1033, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 999.0})
    mock_dl.download_to_file = AsyncMock()

    from bili_unit.processing.audio import Mp3Segment
    seg_files = [
        Mp3Segment(tmp_path / "seg_000.mp3", 0.0, 830.0),
        Mp3Segment(tmp_path / "seg_001.mp3", 830.0, 1033.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="part-A", duration=830.0, model="m"),
        ASRResult(text="part-B", duration=204.0, model="m"),
    ])
    mock_asr.model = "m"
    mock_asr.close = AsyncMock()

    with (
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=seg_files)),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert result["bvid"] == bvid
    assert len(result["pages"]) == 1
    page = result["pages"][0]
    assert page["duration"] == 1033.0
    assert result["total_duration"] == 1033.0
    assert page["text"] == "part-A part-B"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_duration_falls_back_to_segment_sum_when_no_metadata(
    tmp_path, fetching_stack,
):
    from bili_unit.processing.audio import ASRResult

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
            "pages": [{"page_index": 0, "cid": 1, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x"})
    mock_dl.download_to_file = AsyncMock()

    from bili_unit.processing.audio import Mp3Segment
    seg_files = [
        Mp3Segment(tmp_path / "a.mp3", 0.0, 300.0),
        Mp3Segment(tmp_path / "b.mp3", 300.0, 420.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="x", duration=300.0, model="m"),
        ASRResult(text="y", duration=120.0, model="m"),
    ])
    mock_asr.model = "m"

    with (
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=seg_files)),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert result["pages"][0]["duration"] == 420.0
    assert result["total_duration"] == 420.0

    await pd.close()
    await pe.close()


# ---------- ASR resume cache -------------------------------------------------


@pytest.mark.asyncio
async def test_audio_asr_cache_skips_segments_on_retry(tmp_path, fetching_stack):
    from bili_unit.processing.audio import (
        ASRCacheStore,
        ASRResult,
        CachedSegment,
        Mp3Segment,
    )

    fd, _fe, fqry = fetching_stack
    uid = 1010
    bvid = "BVcache"
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

    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    cache.upsert(page, CachedSegment(start_s=0.0, end_s=830.0, text="cached-A", language="auto", duration=830.0, model="m"))
    cache.upsert(page, CachedSegment(start_s=830.0, end_s=1660.0, text="cached-B", language="auto", duration=830.0, model="m"))

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={"bvid": bvid, "pages": [{"page_index": 0, "cid": 1, "duration": 2000, "part": "p1"}]},
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 2000.0})
    mock_dl.download_to_file = AsyncMock()

    seg_files = [
        Mp3Segment(tmp_path / "s0.mp3", 0.0, 830.0),
        Mp3Segment(tmp_path / "s1.mp3", 830.0, 1660.0),
        Mp3Segment(tmp_path / "s2.mp3", 1660.0, 2000.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    transcribe_calls: list[str] = []

    async def fake_transcribe(audio_bytes, mime_type="audio/mp3", language="auto"):
        transcribe_calls.append(language)
        return ASRResult(text="fresh-C", duration=340.0, model="m")

    mock_asr = AsyncMock()
    mock_asr.transcribe = fake_transcribe
    mock_asr.model = "m"

    with (
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=seg_files)),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert len(transcribe_calls) == 1
    text = result["pages"][0]["text"]
    assert "cached-A" in text
    assert "cached-B" in text
    assert "fresh-C" in text
    cache_dir = tmp_path / "proc-asr-cache" / str(uid) / bvid
    assert not cache_dir.exists()

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_asr_cache_persists_on_failure(tmp_path, fetching_stack):
    from bili_unit.processing import ASRAPIError
    from bili_unit.processing.audio import ASRCacheStore, ASRResult, Mp3Segment

    fd, _fe, fqry = fetching_stack
    uid = 1011
    bvid = "BVfail"
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
        item_type="audio", item_id=bvid,
        item_data={"bvid": bvid, "pages": [{"page_index": 0, "cid": 1, "duration": 1660, "part": "p1"}]},
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 1660.0})
    mock_dl.download_to_file = AsyncMock()

    seg_files = [
        Mp3Segment(tmp_path / "s0.mp3", 0.0, 830.0),
        Mp3Segment(tmp_path / "s1.mp3", 830.0, 1660.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    transcribe_results: list = [
        ASRResult(text="part-A", duration=830.0, model="m"),
        ASRAPIError("quota exhausted"),
    ]

    async def fake_transcribe(audio_bytes, mime_type="audio/mp3", language="auto"):
        nxt = transcribe_results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    mock_asr = AsyncMock()
    mock_asr.transcribe = fake_transcribe
    mock_asr.model = "m"

    with (
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=seg_files)),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
        pytest.raises(ASRAPIError),
    ):
        await cmd._runner._do_audio_work(uid, work_item, credential=None)

    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    assert len(page.segments) == 1
    assert page.segments[0].start_s == 0.0
    assert page.segments[0].end_s == 830.0
    assert page.segments[0].text == "part-A"

    await pd.close()
    await pe.close()


@pytest.mark.asyncio
async def test_audio_asr_cache_disabled_bypasses_cache(tmp_path, fetching_stack):
    from bili_unit.processing.audio import ASRResult, Mp3Segment

    fd, _fe, fqry = fetching_stack
    uid = 1012
    bvid = "BVoff"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    s.bili_processing_asr_cache_enabled = False
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
    )

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={"bvid": bvid, "pages": [{"page_index": 0, "cid": 1, "duration": 100, "part": "p1"}]},
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 100.0})
    mock_dl.download_to_file = AsyncMock()

    seg_files = [Mp3Segment(tmp_path / "s0.mp3", 0.0, 100.0)]
    seg_files[0].path.write_bytes(b"x")

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(return_value=ASRResult(text="x", duration=100.0, model="m"))
    mock_asr.model = "m"

    with (
        patch("bili_unit.processing.runner.AudioDownloader", return_value=mock_dl),
        patch("bili_unit.processing.runner.convert_single", new=AsyncMock(return_value=seg_files)),
        patch.object(cmd._runner, "_asr_backend", mock_asr),
    ):
        await cmd._runner._do_audio_work(uid, work_item, credential=None)

    assert not (tmp_path / "proc-asr-cache").exists()

    await pd.close()
    await pe.close()
