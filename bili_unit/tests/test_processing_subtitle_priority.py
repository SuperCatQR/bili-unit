# Tests for the W2.1 audio subtitle short-circuit — Phase 6 SQLite rewrite.
#
# When a bvid's parsed ``video_subtitle`` is complete, the audio runner skips
# ASR and writes a SUCCESS row sourced from the subtitle segments. The
# fallback path (partial / missing subtitle) still runs the ASR worker.
#
# Storage flips from file-KV to per-uid SQLite:
#   * raw video_detail payload → ``FetchingStore.save_raw_payload``
#   * parsed VideoSubtitle      → ``ParsingStore.save_video_subtitle``
#   * audio result              → ``ProcessingStore.save_audio_transcription``
#                                 (read back through ``get_audio_payload``).

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    """Stand-in for the real audio worker — writes an asr-sourced SUCCESS row
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
        "source_endpoints": ["video_detail"],
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

async def _seed_video_detail(
    settings: BiliSettings, uid: int, bvid: str, *, pages_per_bvid: int = 1,
) -> None:
    """Seed the raw video_detail payload + a placeholder ``video`` row."""
    pages_template = [
        {"cid": idx + 1, "part": f"P{idx + 1}", "duration": 60}
        for idx in range(pages_per_bvid)
    ]
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        await store.save_raw_payload("video_detail", bvid, {
            "info": {
                "bvid": bvid, "aid": 0, "title": f"title-{bvid}",
                "desc": "", "duration": 60 * pages_per_bvid,
                "pages": pages_template,
                "stat": {"view": 1, "danmaku": 0, "reply": 0,
                         "favorite": 0, "coin": 0, "share": 0, "like": 1},
                "owner": {"mid": 999, "name": "U"},
            },
            "tags": [],
        })
        # Placeholder video row so audio_transcription FK is satisfied.
        await ctx.main.execute(
            "INSERT OR IGNORE INTO video(bvid, title, payload, parsed_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (bvid, f"title-{bvid}", "{}", 0),
        )
    finally:
        await ctx.close()


async def _seed_parsed_subtitle(
    settings: BiliSettings, uid: int, bvid: str,
    *, page_count: int, missing_pages: list[int] | None = None,
) -> None:
    """Write a VideoSubtitle row to the parsing store.

    ``missing_pages`` (page_index list) — those pages get an empty ``lan``
    so the runner's per-page completeness check (``every page has lan``)
    fails and the audio worker takes over.
    """
    missing = set(missing_pages or [])
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
        pages.append(SubtitlePage(
            page_index=idx, cid=idx + 1,
            lan="zh-CN", lan_doc="中文（中国）",
            is_ai=False,
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
    await ctx.open()
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
    await _seed_video_detail(settings, uid, bvid, pages_per_bvid=1)
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


async def test_subtitle_partial_falls_back_to_asr(cmd, settings):
    """A bvid with one page missing a language is NOT short-circuited;
    the audio worker gets the item and writes ``transcription_source: asr``."""
    uid = 9101
    bvid = "BVpartial"
    await _seed_video_detail(settings, uid, bvid, pages_per_bvid=2)
    # Page 0 has a body; page 1 is dropped → is_complete=False.
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
    """No video_subtitle row → ASR path runs, no short-circuit."""
    uid = 9102
    bvid = "BVnosubt"
    await _seed_video_detail(settings, uid, bvid, pages_per_bvid=1)
    # NB: do NOT seed parsing.video_subtitle.

    result = await cmd.process_uid(uid)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == [bvid]

    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "asr"


async def test_processing_does_not_read_fetching_subtitle_endpoint(
    cmd, settings,
):
    """Processing must NOT touch the raw fetching ``video_subtitle`` endpoint;
    it consumes the parsed subtitle through the parsing store instead.

    Asserted by spying on ``FetchingStore.list_fanout_payloads`` /
    ``FetchingStore.get_raw_payload``: neither is called with the
    ``video_subtitle`` endpoint during a process_uid run.
    """
    uid = 9103
    bvid = "BVcheck"
    await _seed_video_detail(settings, uid, bvid, pages_per_bvid=1)
    await _seed_parsed_subtitle(settings, uid, bvid, page_count=1)

    list_fanout_calls: list[str] = []
    get_raw_calls: list[tuple[str, str]] = []
    orig_list_fanout = FetchingStore.list_fanout_payloads
    orig_get_raw = FetchingStore.get_raw_payload

    async def spy_list_fanout(self, endpoint):
        list_fanout_calls.append(endpoint)
        return await orig_list_fanout(self, endpoint)

    async def spy_get_raw(self, endpoint, item_id=""):
        get_raw_calls.append((endpoint, item_id))
        return await orig_get_raw(self, endpoint, item_id)

    with (
        patch.object(FetchingStore, "list_fanout_payloads", new=spy_list_fanout),
        patch.object(FetchingStore, "get_raw_payload", new=spy_get_raw),
    ):
        await cmd.process_uid(uid)

    # No fetching call asked for the raw subtitle endpoint.
    assert all(ep != "video_subtitle" for ep in list_fanout_calls), \
        f"unexpected fetching call(s): {list_fanout_calls}"
    assert all(ep != "video_subtitle" for ep, _i in get_raw_calls), \
        f"unexpected fetching call(s): {get_raw_calls}"
    # Subtitle short-circuit path still triggered (sanity: the test would be
    # vacuously satisfied if the runner skipped audio entirely).
    payload = await _read_audio_payload(settings, uid, bvid)
    assert payload is not None
    assert payload["result"]["transcription_source"] == "subtitle"
