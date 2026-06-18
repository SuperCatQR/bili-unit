# Integration tests for processing runner / command 鈥?Phase 3.3 SQLite rewrite.
#
# After the SQLite refactor:
#   - ``ProcessingCommand`` builds its own per-uid stores (no ``data`` /
#     ``error`` / ``fetching_query`` / ``parsing_query`` parameters).
#   - Tests seed parsed video/video_page rows into the main DB and read back
#     via ``ProcessingStore.get_audio_status`` / ``get_audio_payload`` /
#     direct SQL on the main DB.

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.processing import (
    ASRAPIError,
    AudioError,
    DownloadError,
    EmptyTranscriptError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from bili_unit.processing._store import ProcessingStore
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.runner import ProcessingRunner
from bili_unit.processing.runner._pipeline_executor import (
    WorkerOutcome,
    WorkItem,
    run_item_workers,
)


def _make_settings(tmp_path, max_retries=3, retry_delays="30,60,120") -> BiliSettings:
    """Fast settings: tiny worker pool, deterministic queue size."""
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=max_retries,
        bili_processing_retry_delays=retry_delays,
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
    )


async def _fake_process_audio_one(runner, uid, item, credential):
    """Stand-in for ProcessingRunner._process_audio_one.

    Writes a fake SUCCESS audio_transcription row through the runner's
    bound store 鈥?same shape as the real success path.
    """
    bvid = item.item_id
    now = int(time.time() * 1000)
    pages = [
        {"page_index": p["page_index"], "cid": p["cid"],
         "duration": 60.0, "text": f"mock transcription for {bvid}",
         "language": "auto", "asr_model": "mock-asr-v0", "segments": []}
        for p in item.item_data.get("pages", [])
    ]
    payload = {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": ProcessingItemStatus.SUCCESS.value,
        "result": {
            "bvid": bvid,
            "pages": pages,
            "total_duration": 60.0,
            "total_chars": len(f"mock transcription for {bvid}"),
            "transcription_source": "MIMO-ASR",
        },
        "source_endpoints": ["video", "video_page"],
        "processed_at": now,
    }
    await runner._store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript=f"mock transcription for {bvid}",
        audio_tokens=0,
        seconds=60,
        cache_hits=0,
        payload=payload,
        processed_at_ms=now,
    )
    return True


# -- seeding helpers --------------------------------------------------------

async def _seed_video_pages(
    settings: BiliSettings, uid: int, bvids: list[str],
    *, pages_per_bvid: int = 1,
) -> None:
    """Write parsed video + video_page rows to the main DB for ``uid``."""
    pages_template = [
        {"cid": idx + 1, "part": f"P{idx + 1}", "duration": 60}
        for idx in range(pages_per_bvid)
    ]
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        for bvid in bvids:
            await ctx.main.execute(
                "INSERT OR IGNORE INTO video"
                "(bvid, aid, title, duration_s, payload, parsed_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bvid,
                    int(bvid[2:]) if bvid[2:].isdigit() else 0,
                    f"title-{bvid}",
                    60 * pages_per_bvid,
                    "{}",
                    0,
                ),
            )
            for idx, page in enumerate(pages_template, start=1):
                await ctx.main.execute(
                    "INSERT OR IGNORE INTO video_page"
                    "(bvid, page_no, cid, part, duration_s) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (bvid, idx, page["cid"], page["part"], page["duration"]),
                )
    finally:
        await ctx.close()


async def _raw_db_exists(settings: BiliSettings, uid: int) -> bool:
    ctx = UidContext(uid, settings.bili_db_dir)
    return ctx.paths.raw_db.exists()


async def _read_audio_payload(settings: BiliSettings, uid: int, bvid: str) -> dict | None:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        return await store.get_audio_payload(bvid)
    finally:
        await ctx.close()


async def _list_audio_items(settings: BiliSettings, uid: int) -> list[dict]:
    """Return every audio_transcription row as a dict (decoded payload)."""
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        rows = await ctx.main.fetch_all(
            "SELECT bvid, status, transcription_source, transcript, "
            "       audio_tokens, seconds, cache_hits, payload, processed_at_ms "
            "FROM audio_transcription ORDER BY bvid",
        )
        out = []
        for r in rows:
            out.append({
                "bvid": r["bvid"],
                "status": r["status"],
                "transcription_source": r["transcription_source"],
                "transcript": r["transcript"],
                "audio_tokens": r["audio_tokens"],
                "seconds": r["seconds"],
                "cache_hits": r["cache_hits"],
                "payload": json.loads(r["payload"]),
                "processed_at_ms": r["processed_at_ms"],
            })
        return out
    finally:
        await ctx.close()


async def _list_processing_errors(settings: BiliSettings, uid: int) -> list[dict]:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        return await store.list_errors()
    finally:
        await ctx.close()


async def _list_stage_events(settings: BiliSettings, uid: int) -> list[dict]:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        rows = await ctx.main.fetch_all(
            "SELECT e.event, e.level, e.stage, e.pipeline, e.item_type, "
            "       e.item_id, e.data_json "
            "FROM stage_event e "
            "JOIN stage_run r ON r.run_id = e.run_id "
            "WHERE r.uid = ? ORDER BY e.id",
            (uid,),
        )
        return [
            {
                "event": row["event"],
                "level": row["level"],
                "stage": row["stage"],
                "pipeline": row["pipeline"],
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "data": json.loads(row["data_json"] or "{}"),
            }
            for row in rows
        ]
    finally:
        await ctx.close()


async def _list_stage_runs(settings: BiliSettings, uid: int) -> list[dict]:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        rows = await ctx.main.fetch_all(
            "SELECT run_id, command, status, args_json, summary_json "
            "FROM stage_run WHERE uid = ? ORDER BY started_at_ms",
            (uid,),
        )
        return [
            {
                "run_id": row["run_id"],
                "command": row["command"],
                "status": row["status"],
                "args": json.loads(row["args_json"] or "{}"),
                "summary": json.loads(row["summary_json"] or "{}"),
            }
            for row in rows
        ]
    finally:
        await ctx.close()


async def _read_processing_task(settings: BiliSettings, uid: int) -> dict | None:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        return await store.get_task()
    finally:
        await ctx.close()


class _NullProgress:
    def __init__(self) -> None:
        self.updates: list[tuple[int, str | None]] = []
        self.closed = False

    def update(self, n: int = 1, *, postfix: str | None = None) -> None:
        self.updates.append((n, postfix))

    def close(self) -> None:
        self.closed = True


# -- fixtures ---------------------------------------------------------------

@pytest_asyncio.fixture
async def settings(tmp_path: Path):
    return _make_settings(tmp_path)


@pytest_asyncio.fixture
async def cmd(settings: BiliSettings):
    """A ProcessingCommand wired with the test settings; spy patched on ``run``."""
    c = ProcessingCommand(
        settings,
        credential_provider=AsyncMock(return_value=None),
    )
    with patch.object(
        ProcessingRunner, "_process_audio_one",
        new=_fake_process_audio_one,
    ):
        yield c
    await c.close()


# ---------- audio pipeline integration ---------------------------------------

@pytest.mark.asyncio
async def test_audio_pipeline_discovers_and_processes(cmd, settings):
    uid = 800
    bvids = ["BVaud1", "BVaud2"]
    await _seed_video_pages(settings, uid, bvids)

    result = await cmd.process_uid(uid, mode="incremental")
    assert result.status == ProcessingTaskStatus.SUCCESS

    rows = await _list_audio_items(settings, uid)
    assert {r["bvid"] for r in rows} == set(bvids)
    for r in rows:
        assert r["status"] == "success"
        assert r["payload"]["result"]["bvid"] == r["bvid"]
        assert len(r["payload"]["result"]["pages"]) >= 1


@pytest.mark.asyncio
async def test_run_item_workers_uses_injected_progress_factory():
    items = [
        WorkItem(item_type="audio", item_id="BV1", item_data={}),
        WorkItem(item_type="audio", item_id="BV2", item_data={}),
    ]
    rollup: dict[str, dict[str, int]] = {}
    progress = _NullProgress()

    async def process_item(item: WorkItem) -> WorkerOutcome:
        return WorkerOutcome(
            bucket="transcription",
            completed=1,
            postfix=f"bvid={item.item_id} ok",
        )

    def progress_factory(*, total: int, label: str) -> _NullProgress:
        assert total == 2
        assert label == "audio uid=1"
        return progress

    await run_item_workers(
        items=items,
        worker_count=1,
        queue_maxsize=2,
        label="audio uid=1",
        rollup=rollup,
        process_item=process_item,
        progress_factory=progress_factory,
    )

    assert rollup == {
        "transcription": {
            "total": 0,
            "completed": 2,
            "failed": 0,
            "skipped": 0,
        },
    }
    assert progress.updates == [
        (1, "bvid=BV1 ok"),
        (1, "bvid=BV2 ok"),
    ]
    assert progress.closed is True


@pytest.mark.asyncio
async def test_audio_pipeline_records_run_observability(cmd, settings):
    uid = 805
    bvids = ["BVobs1"]
    await _seed_video_pages(settings, uid, bvids)

    result = await cmd.process_uid(uid, mode="incremental")
    assert result.status == ProcessingTaskStatus.SUCCESS
    assert result.run_id

    runs = await _list_stage_runs(settings, uid)
    assert len(runs) == 1
    assert runs[0]["run_id"] == result.run_id
    assert runs[0]["command"] == "asr"
    assert runs[0]["status"] == "SUCCESS"
    assert runs[0]["args"]["mode"] == "incremental"
    assert runs[0]["summary"]["status"] == "SUCCESS"
    assert runs[0]["summary"]["candidate_count"] == 1

    events = await _list_stage_events(settings, uid)
    event_names = [event["event"] for event in events]
    assert event_names == [
        "asr.run.started",
        "asr.discovery.completed",
        "asr.item.completed",
        "asr.run.completed",
    ]
    completed = next(e for e in events if e["event"] == "asr.item.completed")
    assert completed["pipeline"] == "audio"
    assert completed["item_type"] == "transcription"
    assert completed["item_id"] == "BVobs1"


@pytest.mark.asyncio
async def test_audio_pipeline_incremental_skip(cmd, settings):
    uid = 801
    await _seed_video_pages(settings, uid, ["BVskip"])

    await cmd.process_uid(uid)
    items1 = await _list_audio_items(settings, uid)
    assert len(items1) == 1
    ts1 = items1[0]["processed_at_ms"]

    r2 = await cmd.process_uid(uid, mode="incremental")
    assert r2.status == ProcessingTaskStatus.SUCCESS

    items2 = await _list_audio_items(settings, uid)
    assert items2[0]["processed_at_ms"] == ts1


@pytest.mark.asyncio
async def test_audio_pipeline_failure_records_error(tmp_path):
    s = _make_settings(tmp_path)
    uid = 802
    await _seed_video_pages(s, uid, ["BVfail"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("audio boom"))

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT

    items = await _list_audio_items(s, uid)
    assert len(items) == 1
    assert items[0]["status"] == "failed"

    errs = await _list_processing_errors(s, uid)
    assert any(e["error_type"] == "RuntimeError" for e in errs)
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_incremental_skip_plus_failure_is_partial(tmp_path):
    s = _make_settings(tmp_path)
    uid = 805
    await _seed_video_pages(s, uid, ["BVdone", "BVfail"])

    cmd1 = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
    )
    with patch.object(
        ProcessingRunner,
        "_process_audio_one",
        new=_fake_process_audio_one,
    ):
        await cmd1.process_uid(uid, only_bvids=["BVdone"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("audio boom"))
    cmd2 = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
    )

    result = await cmd2.process_uid(uid, mode="incremental")

    assert result.status == ProcessingTaskStatus.PARTIAL
    assert result.coverage is not None
    assert result.coverage["success"] == 1
    assert result.coverage["failed"] == 1
    task = await _read_processing_task(s, uid)
    assert task is not None
    assert task["payload"]["pipelines"]["audio"]["status"] == "PARTIAL"
    await cmd1.close()
    await cmd2.close()


@pytest.mark.asyncio
async def test_audio_worker_safety_net_persists_failed_item(tmp_path):
    s = _make_settings(tmp_path)
    uid = 807
    await _seed_video_pages(s, uid, ["BVsafety"])

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
    )
    with patch.object(
        ProcessingRunner,
        "_process_audio_one",
        new=AsyncMock(side_effect=RuntimeError("worker boom")),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    items = await _list_audio_items(s, uid)
    assert len(items) == 1
    assert items[0]["bvid"] == "BVsafety"
    assert items[0]["status"] == "failed"
    errs = await _list_processing_errors(s, uid)
    assert any(
        e["error_type"] == "RuntimeError"
        and e["item_id"] == "BVsafety"
        for e in errs
    )
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_discovery_error_records_stage_error_and_fails(tmp_path):
    s = _make_settings(tmp_path)
    uid = 806

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
    )
    with patch.object(
        ProcessingRunner,
        "_discover_audio_items",
        new=AsyncMock(side_effect=RuntimeError("discovery boom")),
    ):
        result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    assert result.estimate is None
    assert await _list_audio_items(s, uid) == []

    task = await _read_processing_task(s, uid)
    assert task is not None
    assert task["status"] == ProcessingTaskStatus.FAILED_PERMANENT.value
    audio = task["payload"]["pipelines"]["audio"]
    assert audio["status"] == ProcessingPipelineStatus.FAILED_PERMANENT.value
    assert audio["items"]["discovery"]["failed"] == 1

    errs = await _list_processing_errors(s, uid)
    assert len(errs) == 1
    assert errs[0]["pipeline"] == "audio"
    assert errs[0]["item_type"] == "discovery"
    assert errs[0]["item_id"] is None
    assert errs[0]["error_type"] == "RuntimeError"
    assert errs[0]["message"] == "discovery boom"
    assert errs[0]["retryable"] is False

    runs = await _list_stage_runs(s, uid)
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["summary"]["status"] == ProcessingTaskStatus.FAILED_PERMANENT.value
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_pipeline_no_video_detail(cmd, settings):
    uid = 803
    result = await cmd.process_uid(uid)
    assert result.status == ProcessingTaskStatus.SUCCESS
    assert await _list_audio_items(settings, uid) == []


@pytest.mark.asyncio
async def test_audio_pipeline_reads_main_db_without_raw_db(cmd, settings):
    uid = 804
    bvids = ["BVmainOnly"]
    await _seed_video_pages(settings, uid, bvids)
    assert await _raw_db_exists(settings, uid) is False

    result = await cmd.process_uid(uid, dry_run=True)

    assert result.status == ProcessingTaskStatus.DRY_RUN
    assert result.dry_run_candidates == bvids
    assert await _raw_db_exists(settings, uid) is False


# ---------- retry behaviour -------------------------------------------------

@pytest.mark.asyncio
async def test_is_retryable_classification():
    assert ProcessingRunner._is_retryable(DownloadError("cdn")) is True
    assert ProcessingRunner._is_retryable(AudioError("generic audio")) is True
    assert ProcessingRunner._is_retryable(RuntimeError("boom")) is False
    assert ProcessingRunner._is_retryable(ValueError("bad")) is False


@pytest.mark.asyncio
async def test_audio_retry_exhausts_then_fails(tmp_path):
    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    uid = 900
    await _seed_video_pages(s, uid, ["BVretry"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=DownloadError("cdn down"))

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT

    items = await _list_audio_items(s, uid)
    assert len(items) == 1
    assert items[0]["status"] == "failed"

    errs = await _list_processing_errors(s, uid)
    audio_errs = [e for e in errs if e["error_type"] == "DownloadError"]
    assert len(audio_errs) == 3
    # list_errors returns newest-first; reverse to compare in attempt order.
    audio_errs_chrono = list(reversed(audio_errs))
    assert audio_errs_chrono[0]["retryable"] is True
    assert audio_errs_chrono[1]["retryable"] is True
    assert audio_errs_chrono[2]["retryable"] is False

    events = await _list_stage_events(s, uid)
    retry_events = [e for e in events if e["event"] == "asr.item.retry_scheduled"]
    failed_events = [e for e in events if e["event"] == "asr.item.failed"]
    assert [e["data"]["retry"] for e in retry_events] == [1, 2]
    assert len(failed_events) == 1
    assert failed_events[0]["item_id"] == "BVretry"
    assert failed_events[0]["data"]["error_type"] == "DownloadError"

    runs = await _list_stage_runs(s, uid)
    assert runs[0]["status"] == "FAILED"
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_retry_succeeds_after_first_failure(tmp_path):
    from bili_unit.processing.audio import Mp3Segment

    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    uid = 901
    await _seed_video_pages(s, uid, ["BVretryOk"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(
        side_effect=[DownloadError("transient"), {"url": "https://cdn/x", "duration": 60}],
    )
    mock_dl.download_to_file = AsyncMock()

    seg = Mp3Segment(tmp_path / "retry-ok.mp3", 0.0, 60.0)
    seg.path.write_bytes(b"x")
    mock_convert = AsyncMock(return_value=[seg])

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(return_value=type("R", (), {"text": "hi", "duration": 60})())
    mock_asr.model = "mock-asr"
    mock_asr.close = AsyncMock()

    cmd = ProcessingCommand(
        s,
        asr_backend=mock_asr,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    items = await _list_audio_items(s, uid)
    assert len(items) == 1
    assert items[0]["status"] == "success"

    errs = await _list_processing_errors(s, uid)
    audio_errs = [e for e in errs if e["error_type"] == "DownloadError"]
    assert len(audio_errs) == 1
    assert audio_errs[0]["retryable"] is True
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_non_retryable_no_retry(tmp_path):
    s = _make_settings(tmp_path, max_retries=3, retry_delays="0,0,0")
    uid = 902
    await _seed_video_pages(s, uid, ["BVnoRetry"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=RuntimeError("not retryable"))

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    errs = await _list_processing_errors(s, uid)
    assert len(errs) == 1
    assert errs[0]["retryable"] is False
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_zero_max_retries_immediate_fail(tmp_path):
    s = _make_settings(tmp_path, max_retries=0, retry_delays="0")
    uid = 903
    await _seed_video_pages(s, uid, ["BVzero"])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(side_effect=DownloadError("fail"))

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    errs = await _list_processing_errors(s, uid)
    assert len(errs) == 1
    assert errs[0]["retryable"] is False
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_all_empty_transcript_fails_persistently(tmp_path):
    from bili_unit.processing.audio import Mp3Segment

    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    uid = 904
    bvid = "BVemptyAll"
    await _seed_video_pages(s, uid, [bvid])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 3.5})
    mock_dl.download_to_file = AsyncMock()

    seg = Mp3Segment(tmp_path / "tail.mp3", 0.0, 3.5)
    seg.path.write_bytes(b"x")
    mock_convert = AsyncMock(return_value=[seg])

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(
        side_effect=EmptyTranscriptError(
            "MiMo ASR returned empty transcription text; inspect the video manually"
        )
    )
    mock_asr.model = "mock-asr"
    mock_asr.close = AsyncMock()

    cmd = ProcessingCommand(
        s,
        asr_backend=mock_asr,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    assert mock_asr.transcribe.await_count == 1

    rows = await _list_audio_items(s, uid)
    assert len(rows) == 1
    assert rows[0]["bvid"] == bvid
    assert rows[0]["status"] == "failed"
    assert rows[0]["transcript"] is None
    assert rows[0]["payload"]["status"] == ProcessingItemStatus.FAILED.value

    errs = await _list_processing_errors(s, uid)
    assert len(errs) == 1
    assert errs[0]["pipeline"] == "audio"
    assert errs[0]["item_type"] == "transcription"
    assert errs[0]["item_id"] == bvid
    assert errs[0]["error_type"] == "EmptyTranscriptError"
    assert "no non-empty transcript text" in errs[0]["message"]
    assert errs[0]["retryable"] is False
    await cmd.close()


@pytest.mark.asyncio
async def test_audio_legacy_empty_transcript_api_error_no_retry(tmp_path):
    from bili_unit.processing.audio import Mp3Segment

    s = _make_settings(tmp_path, max_retries=2, retry_delays="0,0")
    uid = 905
    bvid = "BVlegacyEmpty"
    await _seed_video_pages(s, uid, [bvid])

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 30})
    mock_dl.download_to_file = AsyncMock()

    seg = Mp3Segment(tmp_path / "empty.mp3", 0.0, 30.0)
    seg.path.write_bytes(b"x")
    mock_convert = AsyncMock(return_value=[seg])

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(
        side_effect=ASRAPIError(
            "MiMo ASR returned empty transcription text; inspect the video manually"
        )
    )
    mock_asr.model = "mock-asr"
    mock_asr.close = AsyncMock()

    cmd = ProcessingCommand(
        s,
        asr_backend=mock_asr,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.FAILED_PERMANENT
    assert mock_asr.transcribe.await_count == 1
    errs = await _list_processing_errors(s, uid)
    assert len(errs) == 1
    assert errs[0]["error_type"] == "EmptyTranscriptError"
    assert errs[0]["retryable"] is False
    await cmd.close()


# ---------- _do_audio_work direct tests --------------------------------------
# These hit the audio mixin's _do_audio_work directly via a patched runner;
# the runner needs a settings object but no stores (the work fn doesn't write).

def _make_unit_runner(settings, *, downloader_factory=None, convert_fn=None,
                     asr_backend=None) -> ProcessingRunner:
    """Build a ProcessingRunner with optional injected pieces 鈥?for unit-style
    tests that exercise the audio_work helpers without a store."""
    return ProcessingRunner(
        settings=settings,
        asr_backend=asr_backend,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=downloader_factory,
        convert_fn=convert_fn,
    )


@pytest.mark.asyncio
async def test_audio_duration_uses_page_metadata_not_last_segment(tmp_path):
    from bili_unit.processing.audio import ASRResult, Mp3Segment

    s = _make_settings(tmp_path)
    uid = 950
    bvid = "BVdurFix"

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

    seg_files = [
        Mp3Segment(tmp_path / "seg_000.mp3", 0.0, 830.0),
        Mp3Segment(tmp_path / "seg_001.mp3", 830.0, 1033.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    mock_convert = AsyncMock(return_value=seg_files)

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="part-A", duration=830.0, model="m"),
        ASRResult(text="part-B", duration=204.0, model="m"),
    ])
    mock_asr.model = "m"
    mock_asr.close = AsyncMock()

    runner = _make_unit_runner(
        s,
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
        asr_backend=mock_asr,
    )
    result = await runner._do_audio_work(uid, work_item, credential=None)

    assert result["bvid"] == bvid
    assert len(result["pages"]) == 1
    page = result["pages"][0]
    assert page["duration"] == 1033.0
    assert result["total_duration"] == 1033.0
    assert page["text"] == "part-A part-B"
    assert mock_convert.await_args.kwargs["max_segment_seconds"] == 120


@pytest.mark.asyncio
async def test_audio_duration_falls_back_to_segment_sum_when_no_metadata(tmp_path):
    from bili_unit.processing.audio import ASRResult, Mp3Segment

    s = _make_settings(tmp_path)
    uid = 951
    bvid = "BVdurSum"

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

    seg_files = [
        Mp3Segment(tmp_path / "a.mp3", 0.0, 300.0),
        Mp3Segment(tmp_path / "b.mp3", 300.0, 420.0),
    ]
    for seg in seg_files:
        seg.path.write_bytes(b"x")

    mock_convert = AsyncMock(return_value=seg_files)

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(side_effect=[
        ASRResult(text="x", duration=300.0, model="m"),
        ASRResult(text="y", duration=120.0, model="m"),
    ])
    mock_asr.model = "m"

    runner = _make_unit_runner(
        s,
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
        asr_backend=mock_asr,
    )
    result = await runner._do_audio_work(uid, work_item, credential=None)

    assert result["pages"][0]["duration"] == 420.0
    assert result["total_duration"] == 420.0


# ---------- ASR resume cache -------------------------------------------------


@pytest.mark.asyncio
async def test_audio_asr_cache_skips_segments_on_retry(tmp_path):
    from bili_unit.processing.audio import (
        ASRCacheStore,
        ASRResult,
        CachedSegment,
        Mp3Segment,
    )

    s = _make_settings(tmp_path)
    uid = 1010
    bvid = "BVcache"

    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    cache.upsert(page, CachedSegment(start_s=0.0, end_s=830.0, text="cached-A", language="auto", duration=830.0, model="m", backend="test-asr"))
    cache.upsert(page, CachedSegment(start_s=830.0, end_s=1660.0, text="cached-B", language="auto", duration=830.0, model="m", backend="test-asr"))

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

    mock_convert = AsyncMock(return_value=seg_files)

    transcribe_calls: list[str] = []

    async def fake_transcribe(audio_bytes, mime_type="audio/mp3", language="auto"):
        transcribe_calls.append(language)
        return ASRResult(text="fresh-C", duration=340.0, model="m")

    mock_asr = AsyncMock()
    mock_asr.transcribe = fake_transcribe
    mock_asr.model = "m"
    mock_asr.cache_namespace = "test-asr"

    runner = _make_unit_runner(
        s,
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
        asr_backend=mock_asr,
    )
    result = await runner._do_audio_work(uid, work_item, credential=None)

    assert len(transcribe_calls) == 1
    text = result["pages"][0]["text"]
    assert "cached-A" in text
    assert "cached-B" in text
    assert "fresh-C" in text
    cache_dir = tmp_path / "proc-asr-cache" / str(uid) / bvid
    assert not cache_dir.exists()


@pytest.mark.asyncio
async def test_audio_asr_cache_persists_on_failure(tmp_path):
    from bili_unit.processing import ASRAPIError
    from bili_unit.processing.audio import ASRCacheStore, ASRResult, Mp3Segment

    s = _make_settings(tmp_path)
    uid = 1011
    bvid = "BVfail"

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

    mock_convert = AsyncMock(return_value=seg_files)

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

    runner = _make_unit_runner(
        s,
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
        asr_backend=mock_asr,
    )

    with pytest.raises(ASRAPIError):
        await runner._do_audio_work(uid, work_item, credential=None)

    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    assert len(page.segments) == 1
    assert page.segments[0].start_s == 0.0
    assert page.segments[0].end_s == 830.0
    assert page.segments[0].text == "part-A"


@pytest.mark.asyncio
async def test_audio_asr_cache_disabled_bypasses_cache(tmp_path):
    from bili_unit.processing.audio import ASRResult, Mp3Segment

    s = _make_settings(tmp_path)
    s.bili_processing_asr_cache_enabled = False
    uid = 1012
    bvid = "BVoff"

    work_item = WorkItem(
        item_type="audio", item_id=bvid,
        item_data={"bvid": bvid, "pages": [{"page_index": 0, "cid": 1, "duration": 100, "part": "p1"}]},
    )

    mock_dl = AsyncMock()
    mock_dl.get_audio_url = AsyncMock(return_value={"url": "https://cdn/x", "duration": 100.0})
    mock_dl.download_to_file = AsyncMock()

    seg_files = [Mp3Segment(tmp_path / "s0.mp3", 0.0, 100.0)]
    seg_files[0].path.write_bytes(b"x")

    mock_convert = AsyncMock(return_value=seg_files)

    mock_asr = AsyncMock()
    mock_asr.transcribe = AsyncMock(return_value=ASRResult(text="x", duration=100.0, model="m"))
    mock_asr.model = "m"

    runner = _make_unit_runner(
        s,
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
        asr_backend=mock_asr,
    )
    await runner._do_audio_work(uid, work_item, credential=None)

    assert not (tmp_path / "proc-asr-cache").exists()


# NOTE: the legacy ``test_processing_video_full_view_legacy_placeholder`` skip
# stub (which referenced the deleted ``BiliQuery``/``ProcessingQuery`` surface)
# was dropped in the Phase 6 rewrite. Aggregate-view assertions now live in
# the SQLite-store contract tests (see test_processing_store_sqlite.py and
# the per-stage SQL view tests).
