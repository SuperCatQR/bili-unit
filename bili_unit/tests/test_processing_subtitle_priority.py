# Tests for the W2.1 audio subtitle short-circuit 鈥?Phase 6 SQLite rewrite.
#
# When a bvid's parsed ``video_subtitle`` is complete, the audio runner skips
# ASR and writes a SUCCESS row sourced from the subtitle segments. The
# fallback path (partial / missing subtitle) still runs the ASR worker.
#
# Storage is per-uid SQLite:
#   * parsed video/video_page    -> main DB
#   * parsed VideoSubtitle       -> ``ParsingStore.save_video_subtitle``
#   * audio result               -> ``ProcessingStore.save_audio_transcription``
#                                  (read back through ``get_audio_payload``).

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.parsing._store import ParsingStore
from bili_unit.parsing.models.video_subtitle import (
    SubtitlePage,
    SubtitleSegment,
    VideoSubtitle,
)
from bili_unit.processing import ProcessingTaskStatus
from bili_unit.processing._store import ProcessingStore
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.runner import ProcessingRunner


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


# Spy: every bvid that gets dispatched into the worker pool is recorded.
_dispatched_bvids: list[str] = []


async def _spy_process_audio_one(runner, uid, item, credential):
    """Stand-in for the real audio worker 鈥?writes an asr-sourced SUCCESS row
    so the test can distinguish "audio worker ran" from "subtitle short-
    circuited" by reading transcription_source from the store."""
    _dispatched_bvids.append(item.item_id)
    bvid = item.item_id
    now = int(time.time() * 1000)
    payload = {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": "SUCCESS",
        "result": {
            "bvid": bvid,
            "pages": [],
            "total_duration": 0.0,
            "total_chars": 0,
            "transcription_source": "asr",
        },
        "source_endpoints": ["video", "video_page"],
        "processed_at": now,
    }
    await runner._store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="asr",
        transcript=None,
        audio_tokens=0,
        seconds=0,
        cache_hits=0,
        payload=payload,
        processed_at_ms=now,
    )
    return True


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

async def _seed_video_pages(
    settings: BiliSettings, uid: int, bvid: str, *, pages_per_bvid: int = 1,
) -> None:
    """Seed parsed video + video_page rows in the main DB."""
    pages_template = [
        {"cid": idx + 1, "part": f"P{idx + 1}", "duration": 60}
        for idx in range(pages_per_bvid)
    ]
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        await ctx.main.execute(
            "INSERT OR IGNORE INTO video"
            "(bvid, title, duration_s, payload, parsed_at_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (bvid, f"title-{bvid}", 60 * pages_per_bvid, "{}", 0),
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


async def _seed_parsed_subtitle(
    settings: BiliSettings, uid: int, bvid: str,
    *, page_count: int, missing_pages: list[int] | None = None,
    ai_pages: list[int] | None = None,
) -> None:
    """Write a VideoSubtitle row to the parsing store.

    ``missing_pages`` (page_index list) 鈥?those pages get an empty ``lan``
    so the runner's per-page completeness check (``every page has lan``)
    fails and the audio worker takes over.
    ``ai_pages`` marks pages as AI subtitles (``lan`` starts with ``ai-``).
    """
    missing = set(missing_pages or [])
    ai = set(ai_pages or [])
    pages: list[SubtitlePage] = []
    for idx in range(page_count):
        if idx in missing:
            pages.append(SubtitlePage(
                page_index=idx, cid=idx + 1,
                lan="", lan_doc="",
                is_ai=False,
                segments=[],
            ))
            continue
        is_ai = idx in ai
        pages.append(SubtitlePage(
            page_index=idx, cid=idx + 1,
            lan="ai-zh" if is_ai else "zh-CN",
            lan_doc="AI涓枃" if is_ai else "涓枃锛堜腑鍥斤級",
            is_ai=is_ai,
            segments=[
                SubtitleSegment(start=0.0, end=1.5, content=f"hello {idx}"),
                SubtitleSegment(start=1.5, end=3.0, content="world"),
            ],
        ))

    sub = VideoSubtitle(
        bvid=bvid,
        pages=pages,
        available_languages=["zh-CN"] if any(p.lan for p in pages) else [],
    )

    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        store = ParsingStore(ctx)
        await store.save_video_subtitle(sub)
    finally:
        await ctx.close()


async def _read_audio_payload(settings: BiliSettings, uid: int, bvid: str) -> dict | None:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        return await store.get_audio_payload(bvid)
    finally:
        await ctx.close()


def _raw_db_exists(settings: BiliSettings, uid: int) -> bool:
    ctx = UidContext(uid, settings.bili_db_dir)
    return ctx.paths.raw_db.exists()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def settings(tmp_path: Path):
    return _make_settings(tmp_path)


@pytest_asyncio.fixture
async def cmd(settings: BiliSettings):
    """Command with the audio-worker spy patched in."""
    c = ProcessingCommand(
        settings,
        credential_provider=AsyncMock(return_value=None),
    )
    _dispatched_bvids.clear()
    with patch.object(
        ProcessingRunner, "_process_audio_one", new=_spy_process_audio_one,
    ):
        yield c
    await c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_subtitle_complete_skips_asr(cmd, settings):
    """A bvid with a complete subtitle is satisfied by the short-circuit:
    no ASR dispatch, ``transcription_source == "subtitle"`` written."""
    uid = 9100
    bvid = "BVsubok"
    await _seed_video_pages(settings, uid, bvid, pages_per_bvid=1)
    await _seed_parsed_subtitle(settings, uid, bvid, page_count=1)

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    # Worker pool was never asked to run anything for this bvid.
    assert _dispatched_bvids == []

    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["status"] == "SUCCESS"
    res = payload["result"]
    assert res["transcription_source"] == "subtitle"
    assert res["bvid"] == bvid
    assert len(res["pages"]) == 1
    page = res["pages"][0]
    assert page["language"] == "zh-CN"
    assert page["asr_model"] == "subtitle"
    assert page["text"].startswith("hello 0")
    # Per-segment shape carries timing + text + model tag.
    assert page["segments"][0]["model"] == "subtitle"
    assert page["segments"][0]["start_s"] == 0.0
    assert page["segments"][0]["end_s"] == 1.5
    assert page["segments"][0]["text"] == "hello 0"
    # Source endpoint reflects the subtitle origin.
    assert payload["source_endpoints"] == ["video_subtitle"]


async def test_ai_subtitle_complete_falls_back_to_asr(cmd, settings):
    """Bilibili AI subtitles are not trusted enough to skip ASR."""
    uid = 9104
    bvid = "BVaisub"
    await _seed_video_pages(settings, uid, bvid, pages_per_bvid=1)
    await _seed_parsed_subtitle(settings, uid, bvid, page_count=1, ai_pages=[0])

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == [bvid]

    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "asr"


async def test_subtitle_partial_falls_back_to_asr(cmd, settings):
    """A bvid with one page missing a language is NOT short-circuited;
    the audio worker gets the item and writes ``transcription_source: asr``."""
    uid = 9101
    bvid = "BVpartial"
    await _seed_video_pages(settings, uid, bvid, pages_per_bvid=2)
    # Page 0 has a body; page 1 is dropped 鈫?is_complete=False.
    await _seed_parsed_subtitle(
        settings, uid, bvid, page_count=2, missing_pages=[1],
    )

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == [bvid]

    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "asr"


async def test_no_subtitle_data_falls_back_to_asr(cmd, settings):
    """No video_subtitle row 鈫?ASR path runs, no short-circuit."""
    uid = 9102
    bvid = "BVnosubt"
    await _seed_video_pages(settings, uid, bvid, pages_per_bvid=1)
    # NB: do NOT seed parsing.video_subtitle.

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == [bvid]

    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "asr"


async def test_processing_does_not_read_fetching_store(cmd, settings):
    """Processing consumes main DB only; raw DB is not opened or created."""
    uid = 9103
    bvid = "BVcheck"
    await _seed_video_pages(settings, uid, bvid, pages_per_bvid=1)
    await _seed_parsed_subtitle(settings, uid, bvid, page_count=1)
    assert _raw_db_exists(settings, uid) is False

    await cmd.process_uid(uid)

    assert _raw_db_exists(settings, uid) is False
    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "subtitle"
