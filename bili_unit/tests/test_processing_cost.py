# Tests for the W3.1 ASR / subtitle cost tracking.
#
# audio result dicts now carry a ``cost`` block with audio_tokens, seconds,
# model, cache_hits, fresh_segments. Cache hits keep accumulating tokens /
# seconds (so the bvid-level cost is stable across runs) while fresh_segments
# / cache_hits expose how the work split. Subtitle short-circuit returns a
# zero-cost block tagged with model="subtitle".

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit._env import BiliSettings
from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.keys import _fetch_key, _item_fetch_key
from bili_unit.fetching.keys import _task_key as _fetch_task_key
from bili_unit.fetching.query import Query as FetchingQuery
from bili_unit.fetching.task import EndpointEntry, TaskValue
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key as _parsing_item_key
from bili_unit.parsing.query import ParsingQuery
from bili_unit.processing import ProcessingItemStatus
from bili_unit.processing.audio import (
    ASRResult,
    Mp3Segment,
)
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _proc_key
from bili_unit.processing.runner._pipeline_executor import WorkItem

# ---------------------------------------------------------------------------
# Settings + fakes
# ---------------------------------------------------------------------------

def _make_settings(tmp_path) -> BiliSettings:
    return BiliSettings(
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=0,
        bili_processing_retry_delays="0",
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


async def _seed_fetching_video_detail(
    fd: FetchingDataStore,
    uid: int,
    bvids: list[str],
    *,
    pages_per_bvid: int = 1,
) -> None:
    pages_template = [
        {"cid": idx + 1, "part": f"P{idx + 1}", "duration": 60}
        for idx in range(pages_per_bvid)
    ]
    tv = TaskValue(
        uid=uid,
        status=TaskStatus.SUCCESS,
        endpoints={
            "video_detail": EndpointEntry(
                status=EndpointStatus.SUCCESS,
                item_progress={
                    "total": len(bvids),
                    "completed": len(bvids),
                    "failed": 0,
                },
            ),
        },
        created_at=0,
        updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())
    await fd.put(_fetch_key(uid, "video_detail"), {
        "uid": uid, "endpoint": "video_detail",
        "status": EndpointStatus.SUCCESS.value,
        "raw_payload": None,
        "item_counts": {
            "total": len(bvids), "completed": len(bvids), "failed": 0,
        },
    })
    for bvid in bvids:
        await fd.put(_item_fetch_key(uid, "video_detail", bvid), {
            "uid": uid, "endpoint": "video_detail", "item_id": bvid,
            "status": EndpointStatus.SUCCESS.value,
            "raw_payload": {
                "info": {
                    "bvid": bvid,
                    "aid": 0,
                    "title": f"title-{bvid}",
                    "desc": "", "duration": 60 * pages_per_bvid,
                    "pages": pages_template,
                    "stat": {"view": 1, "danmaku": 0, "reply": 0,
                             "favorite": 0, "coin": 0, "share": 0, "like": 1},
                    "owner": {"mid": 999, "name": "U"},
                },
                "tags": [],
            },
        })


def _make_segments(tmp_path, ranges):
    """Build mp3 segment placeholders for the given (start, end) tuples."""
    out = []
    for i, (start, end) in enumerate(ranges):
        p = tmp_path / f"seg_{i}.mp3"
        p.write_bytes(b"x")
        out.append(Mp3Segment(p, float(start), float(end)))
    return out


def _make_command(tmp_path, fqry, pd, pe, settings, *, mock_asr, mock_dl, mock_convert):
    return ProcessingCommand(
        data=pd, error=pe, temp_dir=settings.bili_processing_temp_dir,
        fetching_query=fqry, settings=settings,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
    )


# ---------------------------------------------------------------------------
# 1. Fresh ASR — every segment hits the backend → cost accumulates.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_accumulates_for_fresh_asr_segments(tmp_path, fetching_stack):
    fd, _fe, fqry = fetching_stack
    uid = 7000
    bvid = "BVcost1"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={
            "bvid": bvid,
            "pages": [{"page_index": 0, "cid": 1, "duration": 45, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        return_value={"url": "https://cdn/x", "duration": 45.0},
    )
    mock_dl.download_to_file = AsyncMock()

    seg_files = _make_segments(tmp_path, [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0)])
    mock_convert = AsyncMock(return_value=seg_files)

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="a", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="b", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="c", duration=15.0, model="m", audio_tokens=100),
    ])
    mock_asr.model = "m"

    cmd = _make_command(
        tmp_path, fqry, pd, pe, s,
        mock_asr=mock_asr, mock_dl=mock_dl, mock_convert=mock_convert,
    )

    with patch.object(cmd._runner, "_asr_backend", mock_asr):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    cost = result["cost"]
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    assert cost["fresh_segments"] == 3
    assert cost["cache_hits"] == 0
    assert cost["model"] == "m"

    await pd.close()
    await pe.close()


# ---------------------------------------------------------------------------
# 2. Cache hits — second run reuses cached tokens; fresh = 0.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_persists_through_cache_on_second_run(tmp_path, fetching_stack):
    fd, _fe, fqry = fetching_stack
    uid = 7001
    bvid = "BVcost2"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={
            "bvid": bvid,
            "pages": [{"page_index": 0, "cid": 1, "duration": 45, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        return_value={"url": "https://cdn/x", "duration": 45.0},
    )
    mock_dl.download_to_file = AsyncMock()

    # Same seg layout used for both runs.
    def _fresh_segs():
        return _make_segments(tmp_path, [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0)])

    mock_convert_first = AsyncMock(return_value=_fresh_segs())
    mock_asr_first = AsyncMock()
    mock_asr_first.transcribe = AsyncMock(side_effect=[
        ASRResult(text="a", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="b", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="c", duration=15.0, model="m", audio_tokens=100),
    ])
    mock_asr_first.model = "m"

    cmd_first = _make_command(
        tmp_path, fqry, pd, pe, s,
        mock_asr=mock_asr_first, mock_dl=mock_dl, mock_convert=mock_convert_first,
    )
    with patch.object(cmd_first._runner, "_asr_backend", mock_asr_first):
        first = await cmd_first._runner._do_audio_work(
            uid, work_item, credential=None,
        )
    # Sanity: first run paid full cost.
    assert first["cost"]["fresh_segments"] == 3
    assert first["cost"]["audio_tokens"] == 300

    # First run cleared the cache for this bvid (success path); reseed it
    # the way a real failure-then-retry would have left things by running
    # the page through transcribe with a cache-aware backend that records
    # tokens. We simulate this by upserting CachedSegments with audio_tokens
    # ourselves, which is the same path the runner uses on every fresh ASR
    # call.
    from bili_unit.processing.audio import ASRCacheStore, CachedSegment
    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    cache.upsert(page, CachedSegment(0.0, 15.0, "a", "auto", 15.0, "m", audio_tokens=100))
    cache.upsert(page, CachedSegment(15.0, 30.0, "b", "auto", 15.0, "m", audio_tokens=100))
    cache.upsert(page, CachedSegment(30.0, 45.0, "c", "auto", 15.0, "m", audio_tokens=100))

    # Second run: empty backend; everything should hit cache.
    mock_convert_second = AsyncMock(return_value=_fresh_segs())
    mock_asr_second = AsyncMock()
    mock_asr_second.transcribe = AsyncMock(
        side_effect=AssertionError("ASR must not be called on cache-hit run"),
    )
    mock_asr_second.model = "m"

    cmd_second = _make_command(
        tmp_path, fqry, pd, pe, s,
        mock_asr=mock_asr_second, mock_dl=mock_dl, mock_convert=mock_convert_second,
    )
    with patch.object(cmd_second._runner, "_asr_backend", mock_asr_second):
        second = await cmd_second._runner._do_audio_work(
            uid, work_item, credential=None,
        )

    cost = second["cost"]
    # Cache hits accumulate the same audio_tokens as the original run.
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    # No fresh ASR calls; all 3 segments came from cache.
    assert cost["fresh_segments"] == 0
    assert cost["cache_hits"] == 3

    await pd.close()
    await pe.close()


# ---------------------------------------------------------------------------
# 3. Mixed — 2 cache hits + 1 fresh.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_aggregates_mixed_cache_and_fresh(tmp_path, fetching_stack):
    from bili_unit.processing.audio import ASRCacheStore, CachedSegment

    fd, _fe, fqry = fetching_stack
    uid = 7002
    bvid = "BVcost3"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()

    # Pre-seed two cached segments.
    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    cache.upsert(page, CachedSegment(0.0, 15.0, "a", "auto", 15.0, "m", audio_tokens=100))
    cache.upsert(page, CachedSegment(15.0, 30.0, "b", "auto", 15.0, "m", audio_tokens=100))

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={
            "bvid": bvid,
            "pages": [{"page_index": 0, "cid": 1, "duration": 45, "part": "p1"}],
        },
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        return_value={"url": "https://cdn/x", "duration": 45.0},
    )
    mock_dl.download_to_file = AsyncMock()

    seg_files = _make_segments(
        tmp_path, [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0)],
    )
    mock_convert = AsyncMock(return_value=seg_files)

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="c", duration=15.0, model="m", audio_tokens=100),
    ])
    mock_asr.model = "m"

    cmd = _make_command(
        tmp_path, fqry, pd, pe, s,
        mock_asr=mock_asr, mock_dl=mock_dl, mock_convert=mock_convert,
    )

    with patch.object(cmd._runner, "_asr_backend", mock_asr):
        result = await cmd._runner._do_audio_work(uid, work_item, credential=None)

    cost = result["cost"]
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    assert cost["cache_hits"] == 2
    assert cost["fresh_segments"] == 1
    assert cost["model"] == "m"

    await pd.close()
    await pe.close()


# ---------------------------------------------------------------------------
# 4. Subtitle short-circuit — cost is zero / model="subtitle".
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_shortcut_writes_zero_cost(
    tmp_path, fetching_stack, parsing_stack,
):
    fd, _fe, fqry = fetching_stack
    pd_parse, pqry = parsing_stack
    uid = 7003
    bvid = "BVcostSub"
    await _seed_fetching_video_detail(fd, uid, [bvid])

    # Seed a complete subtitle (one page with a lan + segments).
    await pd_parse.put(
        _parsing_item_key(uid, "video_subtitle", bvid),
        {
            "_model_name": "video_subtitle",
            "_schema_version": 1,
            "bvid": bvid,
            "pages": [{
                "page_index": 0, "cid": 1,
                "lan": "zh-CN", "lan_doc": "中文（中国）",
                "segments": [
                    {"start": 0.0, "end": 1.5, "content": "hello"},
                    {"start": 1.5, "end": 3.0, "content": "world"},
                ],
            }],
            "available_languages": ["zh-CN"],
            "is_complete": True,
            "_source_refs": [{"endpoint": "video_subtitle", "item_id": bvid}],
            "_cross_refs": {
                "cvid": None, "opus_id": None,
                "dynamic_id": None, "bvid": bvid,
            },
        },
    )

    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()

    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, parsing_query=pqry, settings=s,
        credential_provider=AsyncMock(return_value=None),
    )

    await cmd.process_uid(uid)

    record = await pd.get(_proc_key(uid, "audio", bvid))
    assert record is not None
    assert record["status"] == ProcessingItemStatus.SUCCESS.value
    res = record["result"]
    assert res["transcription_source"] == "subtitle"

    cost = res["cost"]
    assert cost["audio_tokens"] == 0
    assert cost["seconds"] == 0
    assert cost["model"] == "subtitle"
    assert cost["cache_hits"] == 0
    assert cost["fresh_segments"] == 0

    await pd.close()
    await pe.close()
