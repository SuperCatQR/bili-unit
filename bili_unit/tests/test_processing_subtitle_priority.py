# Tests for the W2.1 audio subtitle short-circuit: when a bvid's parsed
# ``video_subtitle`` is complete, the audio runner skips ASR and writes a
# SUCCESS row sourced from the subtitle segments.

from __future__ import annotations

import time
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
from bili_unit.processing import ProcessingItemStatus, ProcessingTaskStatus
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _proc_key
from bili_unit.processing.runner import ProcessingRunner

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


# Spy: every bvid that gets dispatched into the worker pool is recorded.
_dispatched_bvids: list[str] = []


async def _spy_process_audio_one(runner, uid, item, credential):
    _dispatched_bvids.append(item.item_id)
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
            "pages": [],
            "total_duration": 0.0,
            "total_chars": 0,
            "transcription_source": "asr",
        },
        "source_endpoints": ["video_detail"],
        "processed_at": now,
    })
    return True


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


@pytest_asyncio.fixture
async def proc_stack(tmp_path, fetching_stack, parsing_stack):
    fd, _fe, fqry = fetching_stack
    pd_parse, pqry = parsing_stack
    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry,
        parsing_query=pqry,
        settings=s,
        credential_provider=AsyncMock(return_value=None),
    )

    _dispatched_bvids.clear()
    with patch.object(
        ProcessingRunner, "_process_audio_one",
        new=_spy_process_audio_one,
    ):
        yield cmd, pd, pe, fd, pd_parse

    await pd.close()
    await pe.close()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

async def _seed_fetching_video_detail(
    fd: FetchingDataStore,
    uid: int,
    bvids: list[str],
    pages_per_bvid: int = 1,
) -> None:
    """Populate fetching with SUCCESS video_detail data for ``bvids``."""
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


async def _seed_parsed_subtitle(
    pd_parse: ParsingDataStore,
    uid: int,
    bvid: str,
    *,
    page_count: int,
    missing_pages: list[int] | None = None,
) -> None:
    """Write a VideoSubtitle dict to the parsing store.

    ``missing_pages`` (page_index list) — those pages get an empty ``lan``
    so ``is_complete`` is False.
    """
    missing = set(missing_pages or [])
    pages = []
    for idx in range(page_count):
        if idx in missing:
            pages.append({
                "page_index": idx, "cid": idx + 1, "lan": "", "lan_doc": "",
                "segments": [],
            })
        else:
            pages.append({
                "page_index": idx, "cid": idx + 1,
                "lan": "zh-CN", "lan_doc": "中文（中国）",
                "segments": [
                    {"start": 0.0, "end": 1.5, "content": f"hello {idx}"},
                    {"start": 1.5, "end": 3.0, "content": "world"},
                ],
            })

    is_complete = bool(pages) and all(p["lan"] for p in pages)
    await pd_parse.put(
        _parsing_item_key(uid, "video_subtitle", bvid),
        {
            "_model_name": "video_subtitle",
            "_schema_version": 1,
            "bvid": bvid,
            "pages": pages,
            "available_languages": ["zh-CN"] if is_complete else [],
            "is_complete": is_complete,
            "_source_refs": [
                {"endpoint": "video_subtitle", "item_id": bvid},
            ],
            "_cross_refs": {
                "cvid": None, "opus_id": None, "dynamic_id": None,
                "bvid": bvid,
            },
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_complete_skips_asr(proc_stack, fetching_stack):
    """A bvid with a complete subtitle is satisfied by the short-circuit:
    no ASR dispatch, ``transcription_source == "subtitle"`` written."""
    cmd, pd, _pe, fd, pd_parse = proc_stack
    uid = 9100
    bvid = "BVsubok"
    await _seed_fetching_video_detail(fd, uid, [bvid], pages_per_bvid=1)
    await _seed_parsed_subtitle(pd_parse, uid, bvid, page_count=1)

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    # Worker pool was never asked to run anything for this bvid.
    assert _dispatched_bvids == []

    record = await pd.get(_proc_key(uid, "audio", bvid))
    assert record is not None
    assert record["status"] == ProcessingItemStatus.SUCCESS.value
    res = record["result"]
    assert res["transcription_source"] == "subtitle"
    assert res["bvid"] == bvid
    assert len(res["pages"]) == 1
    page = res["pages"][0]
    assert page["language"] == "zh-CN"
    assert page["asr_model"] == "subtitle"
    assert page["text"].startswith("hello 0")
    # Per-segment shape carries timing + text.
    assert page["segments"][0]["model"] == "subtitle"
    assert page["segments"][0]["start_s"] == 0.0
    assert page["segments"][0]["end_s"] == 1.5
    assert page["segments"][0]["text"] == "hello 0"
    # Source endpoint reflects the subtitle origin.
    assert record["source_endpoints"] == ["video_subtitle"]


@pytest.mark.asyncio
async def test_subtitle_partial_falls_back_to_asr(proc_stack, fetching_stack):
    """A bvid with one page missing a language is NOT short-circuited;
    the audio worker gets the item and writes ``transcription_source: asr``."""
    cmd, pd, _pe, fd, pd_parse = proc_stack
    uid = 9101
    bvid = "BVpartial"
    await _seed_fetching_video_detail(fd, uid, [bvid], pages_per_bvid=2)
    # Page 0 has a lang; page 1 is empty -> is_complete=False.
    await _seed_parsed_subtitle(
        pd_parse, uid, bvid, page_count=2, missing_pages=[1],
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    # Worker DID run for this bvid.
    assert _dispatched_bvids == [bvid]

    record = await pd.get(_proc_key(uid, "audio", bvid))
    assert record is not None
    assert record["result"]["transcription_source"] == "asr"


@pytest.mark.asyncio
async def test_no_subtitle_data_falls_back_to_asr(proc_stack, fetching_stack):
    """No video_subtitle row → ASR path runs, no short-circuit."""
    cmd, pd, _pe, fd, _pd_parse = proc_stack
    uid = 9102
    bvid = "BVnosubt"
    await _seed_fetching_video_detail(fd, uid, [bvid], pages_per_bvid=1)
    # NB: do NOT seed parsing.video_subtitle.

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == [bvid]

    record = await pd.get(_proc_key(uid, "audio", bvid))
    assert record is not None
    assert record["result"]["transcription_source"] == "asr"


@pytest.mark.asyncio
async def test_processing_does_not_read_fetching_subtitle_endpoint(
    proc_stack, fetching_stack,
):
    """Processing must NOT touch the raw fetching ``video_subtitle`` endpoint;
    it consumes the parsed subtitle through ``parsing.query`` instead.

    Asserted by spying on ``FetchingQuery.list_fanout_payloads`` /
    ``FetchingQuery.get_item``: neither is called with the
    ``video_subtitle`` endpoint during a process_uid run.
    """
    cmd, _pd, _pe, fd, pd_parse = proc_stack
    uid = 9103
    bvid = "BVcheck"
    await _seed_fetching_video_detail(fd, uid, [bvid], pages_per_bvid=1)
    await _seed_parsed_subtitle(pd_parse, uid, bvid, page_count=1)

    fqry = cmd._runner._fetch_qry
    list_fanout_calls: list[tuple] = []
    get_item_calls: list[tuple] = []
    orig_list_fanout = fqry.list_fanout_payloads
    orig_get_item = fqry.get_item

    async def spy_list_fanout(uid_arg, endpoint):
        list_fanout_calls.append((uid_arg, endpoint))
        return await orig_list_fanout(uid_arg, endpoint)

    async def spy_get_item(uid_arg, endpoint, item_id):
        get_item_calls.append((uid_arg, endpoint, item_id))
        return await orig_get_item(uid_arg, endpoint, item_id)

    with (
        patch.object(fqry, "list_fanout_payloads", new=spy_list_fanout),
        patch.object(fqry, "get_item", new=spy_get_item),
    ):
        await cmd.process_uid(uid)

    # No fetching call asked for the raw subtitle endpoint.
    assert all(ep != "video_subtitle" for _u, ep in list_fanout_calls), \
        f"unexpected fetching call(s): {list_fanout_calls}"
    assert all(ep != "video_subtitle" for _u, ep, _i in get_item_calls), \
        f"unexpected fetching call(s): {get_item_calls}"
