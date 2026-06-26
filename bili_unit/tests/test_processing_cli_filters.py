# Tests for the W1.3 asr CLI flags (--limit / --only-bvids /
# --retry-failed-only / --dry-run), exercised end-to-end through
# ``ProcessingCommand.process_uid`` �?Phase 6 SQLite rewrite.
#
# The audio worker is patched to a fast in-memory stand-in (same pattern as
# ``test_processing_runner.py``) so we can assert which bvids actually
# entered the pipeline without running ffmpeg / network / ASR. The spy
# writes a SUCCESS row through ``ProcessingStore.save_audio_transcription``
# rather than the old file-KV.

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
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


# Shared spy: every bvid that hits the worker pool is recorded so the
# test can assert which subset actually entered.
_dispatched_bvids: list[str] = []


async def _spy_process_audio_one(runner, uid, item, credential):
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
        },
        "source_endpoints": ["video", "video_page"],
        "processed_at": now,
    }
    await runner._store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
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
    settings: BiliSettings,
    uid: int,
    bvids: list[str],
) -> None:
    """Write raw video_detail payloads so the runner can discover bvids."""
    import json as _json

    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        for bvid in bvids:
            payload = _json.dumps(
                {
                    "info": {
                        "bvid": bvid,
                        "title": f"title-{bvid}",
                        "duration": 60,
                        "pages": [
                            {
                                "cid": 1,
                                "part": "P1",
                                "duration": 60,
                            }
                        ],
                    },
                }
            )
            await ctx.conn.execute(
                "INSERT OR REPLACE INTO raw_payload(endpoint, item_id, payload, fetched_at_ms) VALUES (?, ?, ?, ?)",
                ("video_detail", bvid, payload, 1),
            )
    finally:
        await ctx.close()


async def _seed_audio_status(
    settings: BiliSettings,
    uid: int,
    bvid: str,
    status: str,
) -> None:
    """Plant an existing audio_transcription row (used for retry-failed-only).

    ``status`` must be one of the audio_transcription CHECK values
    (``success`` / ``failed`` / ``skipped`` / etc.).
    """
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        await store.save_audio_transcription(
            bvid,
            status=status,
            transcription_source=None,
            transcript=None,
            audio_tokens=None,
            seconds=None,
            cache_hits=None,
            payload={
                "uid": uid,
                "pipeline": "audio",
                "item_type": "transcription",
                "item_id": bvid,
                "status": status.upper(),
                "result": None,
                "source_endpoints": ["video", "video_page"],
                "processed_at": 0,
            },
            processed_at_ms=0,
        )
    finally:
        await ctx.close()


async def _read_processing_task(
    settings: BiliSettings,
    uid: int,
) -> dict | None:
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    try:
        store = ProcessingStore(ctx)
        return await store.get_task()
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
    """Command with the ``_process_audio_one`` spy in place."""
    c = ProcessingCommand(
        settings,
        credential_provider=AsyncMock(return_value=None),
    )
    _dispatched_bvids.clear()
    with patch.object(
        ProcessingRunner,
        "_process_audio_one",
        new=_spy_process_audio_one,
    ):
        yield c
    await c.close()


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


async def test_only_bvids_filters_to_explicit_set(cmd, settings):
    """``--only-bvids BV1 BV2`` enters exactly that pair from a 5-item set."""
    uid = 8000
    all_bvids = ["BVone1", "BVtwo2", "BVthree3", "BVfour4", "BVfive5"]
    await _seed_video_pages(settings, uid, all_bvids)

    result = await cmd.process_uid(uid, only_bvids=["BVone1", "BVthree3"])

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert sorted(_dispatched_bvids) == ["BVone1", "BVthree3"]


async def test_limit_caps_to_first_n(cmd, settings):
    """``--limit 2`` truncates a 5-bvid discovery to the first 2."""
    uid = 8001
    all_bvids = ["BVa1", "BVb2", "BVc3", "BVd4", "BVe5"]
    await _seed_video_pages(settings, uid, all_bvids)

    result = await cmd.process_uid(uid, limit=2)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert len(_dispatched_bvids) == 2
    # Discovery preserves the order returned by ``list_fanout_payloads`` (sorted);
    # the survivors must be a subset of the seeded population.
    assert set(_dispatched_bvids).issubset(set(all_bvids))


async def test_only_bvids_then_limit(cmd, settings):
    """``only_bvids`` filters first, then ``limit`` caps the survivors."""
    uid = 8002
    all_bvids = ["BVa1", "BVb2", "BVc3", "BVd4", "BVe5"]
    await _seed_video_pages(settings, uid, all_bvids)

    result = await cmd.process_uid(
        uid,
        only_bvids=["BVb2", "BVd4", "BVe5"],
        limit=2,
    )

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert len(_dispatched_bvids) == 2
    assert set(_dispatched_bvids).issubset({"BVb2", "BVd4", "BVe5"})


async def test_exclude_bvids_filters_out_explicit_set(cmd, settings):
    uid = 8007
    all_bvids = ["BVa1", "BVb2", "BVc3"]
    await _seed_video_pages(settings, uid, all_bvids)

    result = await cmd.process_uid(uid, exclude_bvids=["BVb2"])

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert sorted(_dispatched_bvids) == ["BVa1", "BVc3"]


async def test_retry_failed_only_picks_failed_records(cmd, settings):
    """3 success + 2 failed �?only the 2 failed enter the worker."""
    uid = 8003
    bvids = ["BVok1", "BVok2", "BVok3", "BVfail1", "BVfail2"]
    await _seed_video_pages(settings, uid, bvids)

    for bvid in ["BVok1", "BVok2", "BVok3"]:
        await _seed_audio_status(settings, uid, bvid, "success")
    for bvid in ["BVfail1", "BVfail2"]:
        await _seed_audio_status(settings, uid, bvid, "failed")

    result = await cmd.process_uid(uid, retry_failed_only=True)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert sorted(_dispatched_bvids) == ["BVfail1", "BVfail2"]


async def test_retry_failed_only_reports_missing_coverage(cmd, settings):
    uid = 8008
    await _seed_video_pages(settings, uid, ["BVok1", "BVmissing1"])
    await _seed_audio_status(settings, uid, "BVok1", "success")

    result = await cmd.process_uid(uid, retry_failed_only=True)

    assert result.status == ProcessingTaskStatus.PARTIAL
    assert _dispatched_bvids == []
    assert result.coverage is not None
    assert result.coverage["success"] == 1
    assert result.coverage["expected"] == 2
    assert result.coverage["missing_bvids"] == ["BVmissing1"]

    task = await _read_processing_task(settings, uid)
    assert task is not None
    audio = task["payload"]["pipelines"]["audio"]
    assert audio["status"] == "PARTIAL"
    assert audio["coverage"]["missing_bvids"] == ["BVmissing1"]


async def test_dry_run_skips_worker_dispatch(cmd, settings):
    """Dry-run discovers candidates without mutating ASR task progress."""
    uid = 8004
    bvids = ["BVdr1", "BVdr2", "BVdr3"]
    await _seed_video_pages(settings, uid, bvids)

    result = await cmd.process_uid(uid, dry_run=True)

    assert result.status == ProcessingTaskStatus.DRY_RUN
    assert _dispatched_bvids == []
    assert result.dry_run_candidates is not None
    assert sorted(result.dry_run_candidates) == sorted(bvids)

    task = await _read_processing_task(settings, uid)
    assert task is None

    assert result.estimate == {
        "item_count": 3,
        "page_count": 3,
        "audio_seconds": 180.0,
        "audio_tokens": 1170,
    }


async def test_dry_run_with_no_videos_returns_success(cmd):
    """Dry-run on a uid with no parsed video rows returns empty candidates."""
    uid = 8005

    result = await cmd.process_uid(uid, dry_run=True)

    assert result.status == ProcessingTaskStatus.DRY_RUN
    assert result.dry_run_candidates == []
    assert _dispatched_bvids == []
    assert result.estimate == {
        "item_count": 0,
        "page_count": 0,
        "audio_seconds": 0.0,
        "audio_tokens": 0,
    }


async def test_audio_budget_stops_before_dispatch(cmd, settings):
    uid = 8006
    bvids = ["BVbudget1", "BVbudget2"]
    await _seed_video_pages(settings, uid, bvids)

    result = await cmd.process_uid(uid, max_audio_seconds=30)

    assert result.status == ProcessingTaskStatus.PARTIAL
    assert _dispatched_bvids == []
    assert result.budget_exceeded == ["audio_seconds"]
    assert sorted(result.dry_run_candidates or []) == sorted(bvids)
    assert result.estimate == {
        "item_count": 2,
        "page_count": 2,
        "audio_seconds": 120.0,
        "audio_tokens": 780,
    }


# ---------------------------------------------------------------------------
# Argparse layer
# ---------------------------------------------------------------------------


def test_cli_argparse_accepts_asr_flags():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "asr",
            "1234",
            "--limit",
            "5",
            "--include",
            "BV1",
            "BV2",
            "--dry-run",
            "--max-audio-seconds",
            "60",
            "--max-audio-tokens",
            "500",
        ]
    )
    assert args.command == "asr"
    assert args.limit == 5
    assert args.only_bvids == ["BV1", "BV2"]
    assert args.dry_run is True
    assert args.max_audio_seconds == 60
    assert args.max_audio_tokens == 500
    assert args.retry_failed_only is False


def test_cli_argparse_accepts_legacy_only_bvids_and_exclude_bvids():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["asr", "1234", "--only-bvids", "BV1", "BV2"])
    assert args.only_bvids == ["BV1", "BV2"]
    assert args.exclude_bvids is None

    args = parser.parse_args(["asr", "1234", "--exclude", "BV3"])
    assert args.only_bvids is None
    assert args.exclude_bvids == ["BV3"]

    with pytest.raises(SystemExit):
        parser.parse_args(["asr", "1234", "-e", "BV1", "-x", "BV2"])


def test_cli_argparse_retry_failed_only_flag():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["asr", "1234", "--retry-failed-only"])
    assert args.retry_failed_only is True


def test_cli_argparse_process_alias_removed():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["process", "1234", "--dry-run"])


def test_cli_argparse_rejects_unimplemented_whisper_backend():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["asr", "1234", "--asr-backend", "whisper"])


def test_cli_argparse_rejects_nonpositive_numeric_limits():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    for flag in ("--limit", "--max-audio-seconds", "--max-audio-tokens"):
        with pytest.raises(SystemExit):
            parser.parse_args(["asr", "1234", flag, "0"])


def test_cli_argparse_retry_failed_only_conflicts_with_full():
    """``--retry-failed-only`` + ``--mode full`` is rejected at runtime."""
    import asyncio

    from bili_unit.__main__ import _build_parser, _handle_asr

    parser = _build_parser()
    args = parser.parse_args(
        [
            "asr",
            "1234",
            "--retry-failed-only",
            "--mode",
            "full",
        ]
    )
    with pytest.raises(SystemExit):
        asyncio.run(_handle_asr(args))
