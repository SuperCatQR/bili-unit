# Tests for the W3.1 ASR / subtitle cost tracking — Phase 6 SQLite rewrite.
#
# Audio result dicts carry a ``cost`` block with audio_tokens, seconds,
# model, cache_hits, fresh_segments. Cache hits keep accumulating tokens /
# seconds (so the bvid-level cost is stable across runs) while fresh_segments
# / cache_hits expose how the work split. Subtitle short-circuit returns a
# zero-cost block tagged with model="subtitle".
#
# Tests 1-3 exercise ``_do_audio_work`` directly with a unit-style runner
# (no store wiring needed — _do_audio_work returns the result dict; the
# rollup writer is a separate seam tested in test_processing_runner).
#
# Test 4 exercises ``process_uid`` end-to-end: the subtitle short-circuit
# writes its zero-cost result through ``ProcessingStore.save_audio_transcription``,
# and the assertion reads it back from the SQLite store.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching._store import FetchingStore
from bili_unit.parsing._store import ParsingStore
from bili_unit.parsing.models.video_subtitle import (
    SubtitlePage,
    SubtitleSegment,
    VideoSubtitle,
)
from bili_unit.processing._store import ProcessingStore
from bili_unit.processing.audio import (
    ASRCacheStore,
    ASRResult,
    CachedSegment,
    Mp3Segment,
)
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.runner import ProcessingRunner
from bili_unit.processing.runner._pipeline_executor import WorkItem

# ---------------------------------------------------------------------------
# Settings + helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path) -> BiliSettings:
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=0,
        bili_processing_retry_delays="0",
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
    )


def _make_segments(tmp_path, ranges):
    """Build mp3 segment placeholders for the given (start, end) tuples."""
    out = []
    for i, (start, end) in enumerate(ranges):
        p = tmp_path / f"seg_{i}.mp3"
        p.write_bytes(b"x")
        out.append(Mp3Segment(p, float(start), float(end)))
    return out


def _make_unit_runner(settings, *, mock_asr, mock_dl, mock_convert) -> ProcessingRunner:
    """Build a runner with injected pieces — ``_do_audio_work`` reads them
    from ``self``; no store wiring needed for cost-block assertions."""
    return ProcessingRunner(
        settings=settings,
        asr_backend=mock_asr,
        credential_provider=AsyncMock(return_value=None),
        downloader_factory=lambda credential=None: mock_dl,
        convert_fn=mock_convert,
    )


# ---------------------------------------------------------------------------
# 1. Fresh ASR — every segment hits the backend → cost accumulates.
# ---------------------------------------------------------------------------

async def test_cost_accumulates_for_fresh_asr_segments(tmp_path: Path):
    s = _make_settings(tmp_path)
    uid = 7000
    bvid = "BVcost1"

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

    runner = _make_unit_runner(
        s, mock_asr=mock_asr, mock_dl=mock_dl, mock_convert=mock_convert,
    )
    result = await runner._do_audio_work(uid, work_item, credential=None)

    cost = result["cost"]
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    assert cost["fresh_segments"] == 3
    assert cost["cache_hits"] == 0
    assert cost["model"] == "m"


# ---------------------------------------------------------------------------
# 2. Cache hits — second run reuses cached tokens; fresh = 0.
# ---------------------------------------------------------------------------

async def test_cost_persists_through_cache_on_second_run(tmp_path: Path):
    s = _make_settings(tmp_path)
    uid = 7001
    bvid = "BVcost2"

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

    def _fresh_segs():
        return _make_segments(tmp_path, [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0)])

    # First run — pay the full cost up front.
    mock_convert_first = AsyncMock(return_value=_fresh_segs())
    mock_asr_first = AsyncMock()
    mock_asr_first.transcribe = AsyncMock(side_effect=[
        ASRResult(text="a", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="b", duration=15.0, model="m", audio_tokens=100),
        ASRResult(text="c", duration=15.0, model="m", audio_tokens=100),
    ])
    mock_asr_first.model = "m"

    runner_first = _make_unit_runner(
        s, mock_asr=mock_asr_first, mock_dl=mock_dl, mock_convert=mock_convert_first,
    )
    first = await runner_first._do_audio_work(uid, work_item, credential=None)
    assert first["cost"]["fresh_segments"] == 3
    assert first["cost"]["audio_tokens"] == 300

    # _do_audio_work clears the cache on success; reseed it the way a real
    # failure-then-retry would have — ASRCacheStore upsert is the same path
    # the runner uses on every fresh ASR call.
    cache = ASRCacheStore(s.bili_processing_asr_cache_dir)
    page = cache.load_page(uid, bvid, 0)
    cache.upsert(page, CachedSegment(0.0, 15.0, "a", "auto", 15.0, "m", audio_tokens=100))
    cache.upsert(page, CachedSegment(15.0, 30.0, "b", "auto", 15.0, "m", audio_tokens=100))
    cache.upsert(page, CachedSegment(30.0, 45.0, "c", "auto", 15.0, "m", audio_tokens=100))

    # Second run — empty backend; everything must hit cache.
    mock_convert_second = AsyncMock(return_value=_fresh_segs())
    mock_asr_second = AsyncMock()
    mock_asr_second.transcribe = AsyncMock(
        side_effect=AssertionError("ASR must not be called on cache-hit run"),
    )
    mock_asr_second.model = "m"

    runner_second = _make_unit_runner(
        s, mock_asr=mock_asr_second, mock_dl=mock_dl,
        mock_convert=mock_convert_second,
    )
    second = await runner_second._do_audio_work(uid, work_item, credential=None)

    cost = second["cost"]
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    assert cost["fresh_segments"] == 0
    assert cost["cache_hits"] == 3


# ---------------------------------------------------------------------------
# 3. Mixed — 2 cache hits + 1 fresh.
# ---------------------------------------------------------------------------

async def test_cost_aggregates_mixed_cache_and_fresh(tmp_path: Path):
    s = _make_settings(tmp_path)
    uid = 7002
    bvid = "BVcost3"

    # Pre-seed two cached segments — third one will be fresh.
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

    runner = _make_unit_runner(
        s, mock_asr=mock_asr, mock_dl=mock_dl, mock_convert=mock_convert,
    )
    result = await runner._do_audio_work(uid, work_item, credential=None)

    cost = result["cost"]
    assert cost["audio_tokens"] == 300
    assert cost["seconds"] == 45
    assert cost["cache_hits"] == 2
    assert cost["fresh_segments"] == 1
    assert cost["model"] == "m"


# ---------------------------------------------------------------------------
# 4. Subtitle short-circuit — cost is zero / model="subtitle".
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def _seeded_subtitle(tmp_path: Path):
    """Seed a uid with one bvid carrying both a video_detail raw payload and
    a complete VideoSubtitle parsed row — the precondition for the audio
    short-circuit. Yields ``(settings, uid, bvid)``."""
    s = _make_settings(tmp_path)
    uid = 7003
    bvid = "BVcostSub"

    ctx = UidContext(uid, s.bili_db_dir)
    await ctx.open()
    try:
        fetch_store = FetchingStore(ctx)
        parse_store = ParsingStore(ctx)

        await fetch_store.save_raw_payload("video_detail", bvid, {
            "info": {
                "bvid": bvid, "aid": 0, "title": f"title-{bvid}",
                "desc": "", "duration": 60,
                "pages": [{"cid": 1, "part": "P1", "duration": 60}],
                "stat": {"view": 1, "danmaku": 0, "reply": 0,
                         "favorite": 0, "coin": 0, "share": 0, "like": 1},
                "owner": {"mid": 999, "name": "U"},
            },
            "tags": [],
        })

        # Placeholder video row to satisfy the audio_transcription FK.
        await ctx.main.execute(
            "INSERT OR IGNORE INTO video(bvid, title, payload, parsed_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (bvid, f"title-{bvid}", "{}", 0),
        )

        # Complete VideoSubtitle (one page with a body) — drives the short-circuit.
        sub = VideoSubtitle(
            bvid=bvid,
            pages=[
                SubtitlePage(
                    page_index=0, cid=1,
                    lan="zh-CN", lan_doc="中文（中国）",
                    is_ai=False,
                    segments=[
                        SubtitleSegment(start=0.0, end=1.5, content="hello"),
                        SubtitleSegment(start=1.5, end=3.0, content="world"),
                    ],
                ),
            ],
            available_languages=["zh-CN"],
        )
        await parse_store.save_video_subtitle(sub)
    finally:
        await ctx.close()

    yield s, uid, bvid


async def test_subtitle_shortcut_writes_zero_cost(_seeded_subtitle):
    s, uid, bvid = _seeded_subtitle

    cmd = ProcessingCommand(
        s,
        credential_provider=AsyncMock(return_value=None),
    )
    try:
        await cmd.process_uid(uid)
    finally:
        await cmd.close()

    # Read back the audio_transcription row through the store.
    ctx = UidContext(uid, s.bili_db_dir)
    await ctx.open()
    try:
        proc_store = ProcessingStore(ctx)
        status = await proc_store.get_audio_status(bvid)
        payload = await proc_store.get_audio_payload(bvid)

        # Typed columns also reflect the subtitle short-circuit.
        row = await ctx.main.fetch_one(
            "SELECT transcription_source, audio_tokens, seconds, cache_hits "
            "FROM audio_transcription WHERE bvid = ?",
            (bvid,),
        )
    finally:
        await ctx.close()

    assert status == "success"
    assert payload is not None
    res = payload["result"]
    assert res["transcription_source"] == "subtitle"

    cost = res["cost"]
    assert cost["audio_tokens"] == 0
    assert cost["seconds"] == 0
    assert cost["model"] == "subtitle"
    assert cost["cache_hits"] == 0
    assert cost["fresh_segments"] == 0

    # Typed columns mirror the cost block (the store decomposes them).
    assert row is not None
    assert row["transcription_source"] == "subtitle"
    assert row["audio_tokens"] == 0
    assert row["seconds"] == 0
    assert row["cache_hits"] == 0
