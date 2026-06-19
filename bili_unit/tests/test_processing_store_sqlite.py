# Contract tests for ProcessingStore -- the SQLite-backed write store that
# replaces processing/data.py's ProcessingDataStore + processing/error.py's
# ProcessingErrorStore.
#
# These tests build their own UidContext on tmp_path; they intentionally
# don't reuse the legacy ``stores`` fixture (which still wraps the old
# file-directory implementation).
#
# FK note:
#   ``audio_transcription.bvid`` references ``video.bvid`` ON DELETE CASCADE.
#   Each test that calls ``save_audio_transcription`` first inserts a
#   placeholder ``video`` row via the ``_seed_video`` helper. In production
#   the live pipeline always has a parsed video row before processing runs.

from __future__ import annotations

import json
from pathlib import Path

import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit.processing._store import ProcessingStore

UID = 42


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    ctx = UidContext(uid=UID, root=tmp_path)
    await ctx.open()
    s = ProcessingStore(ctx)
    try:
        yield s
    finally:
        await ctx.close()


async def _seed_video(store: ProcessingStore, bvid: str) -> None:
    """Backwards-compat shim. The new schema has no `video` / `video_page`
    tables and `audio_transcription` no longer FKs to them, so seeding is a
    no-op. Kept so existing tests can call it without churning each call site.
    """
    _ = store, bvid


# ---------------------------------------------------------------------------
# save_audio_transcription
# ---------------------------------------------------------------------------

async def test_save_audio_transcription_success_columns_and_payload(
    store: ProcessingStore,
) -> None:
    bvid = "BV1abc"
    await _seed_video(store, bvid)
    payload = {
        "uid": UID,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": "SUCCESS",
        "result": {
            "transcription_source": "MIMO-ASR",
            "pages": [
                {
                    "page_index": 0,
                    "cid": 123,
                    "duration": 12.5,
                    "text": "hello world",
                    "language": "zh",
                    "asr_model": "mimo-v2.5-asr",
                    "segments": [
                        {
                            "start_s": 0.0,
                            "end_s": 1.5,
                            "duration": 1.5,
                            "text": "hello",
                            "language": "zh",
                            "model": "mimo-v2.5-asr",
                        },
                        {
                            "start_s": 1.5,
                            "end_s": 3.0,
                            "duration": 1.5,
                            "text": "world",
                            "language": "zh",
                            "model": "mimo-v2.5-asr",
                            "high_risk_skip": True,
                            "error": "risk",
                        },
                    ],
                },
            ],
        },
    }
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript="hello world",
        audio_tokens=1234,
        seconds=12.5,
        cache_hits=2,
        payload=payload,
        processed_at_ms=10_000,
    )

    row = await store.ctx.conn.fetch_one(
        "SELECT bvid, status, transcription_source, transcript, "
        "       audio_tokens, seconds, cache_hits, payload, processed_at_ms "
        "FROM audio_transcription WHERE bvid = ?",
        (bvid,),
    )
    assert row is not None
    assert row["bvid"] == bvid
    assert row["status"] == "success"
    assert row["transcription_source"] == "MIMO-ASR"
    assert row["transcript"] == "hello world"
    assert row["audio_tokens"] == 1234
    assert row["seconds"] == 12.5
    assert row["cache_hits"] == 2
    assert json.loads(row["payload"]) == payload
    assert row["processed_at_ms"] == 10_000
    page = await store.ctx.conn.fetch_one(
        "SELECT page_no, page_index, cid, duration_s, language, asr_model, "
        "       transcript_text, transcript_char_count, segment_count "
        "FROM audio_transcription_page WHERE bvid = ?",
        (bvid,),
    )
    assert page is not None
    assert dict(page) == {
        "page_no": 1,
        "page_index": 0,
        "cid": 123,
        "duration_s": 12.5,
        "language": "zh",
        "asr_model": "mimo-v2.5-asr",
        "transcript_text": "hello world",
        "transcript_char_count": len("hello world"),
        "segment_count": 2,
    }
    segments = await store.ctx.conn.fetch_all(
        "SELECT page_no, segment_no, start_seconds, end_seconds, duration_s, "
        "       transcript_text, language, asr_model, "
        "       is_empty_transcript_skip, is_high_risk_audio_skip, error_message "
        "FROM audio_transcription_segment WHERE bvid = ? ORDER BY segment_no",
        (bvid,),
    )
    assert [dict(r) for r in segments] == [
        {
            "page_no": 1,
            "segment_no": 1,
            "start_seconds": 0.0,
            "end_seconds": 1.5,
            "duration_s": 1.5,
            "transcript_text": "hello",
            "language": "zh",
            "asr_model": "mimo-v2.5-asr",
            "is_empty_transcript_skip": 0,
            "is_high_risk_audio_skip": 0,
            "error_message": None,
        },
        {
            "page_no": 1,
            "segment_no": 2,
            "start_seconds": 1.5,
            "end_seconds": 3.0,
            "duration_s": 1.5,
            "transcript_text": "world",
            "language": "zh",
            "asr_model": "mimo-v2.5-asr",
            "is_empty_transcript_skip": 0,
            "is_high_risk_audio_skip": 1,
            "error_message": "risk",
        },
    ]


async def test_save_audio_transcription_failed_no_transcript(
    store: ProcessingStore,
) -> None:
    bvid = "BV2def"
    await _seed_video(store, bvid)
    payload = {"item_id": bvid, "status": "FAILED"}
    await store.save_audio_transcription(
        bvid,
        status="failed",
        transcription_source=None,
        transcript=None,
        audio_tokens=None,
        seconds=None,
        cache_hits=None,
        payload=payload,
    )

    row = await store.ctx.conn.fetch_one(
        "SELECT status, transcription_source, transcript, audio_tokens, "
        "       seconds, cache_hits, processed_at_ms "
        "FROM audio_transcription WHERE bvid = ?",
        (bvid,),
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["transcription_source"] is None
    assert row["transcript"] is None
    assert row["audio_tokens"] is None
    assert row["seconds"] is None
    assert row["cache_hits"] is None
    # processed_at_ms defaults to wall clock
    assert row["processed_at_ms"] > 0


async def test_save_audio_transcription_skipped(store: ProcessingStore) -> None:
    bvid = "BV3skip"
    await _seed_video(store, bvid)
    await store.save_audio_transcription(
        bvid,
        status="skipped",
        transcription_source=None,
        transcript=None,
        audio_tokens=None,
        seconds=None,
        cache_hits=None,
        payload={"item_id": bvid, "status": "SKIPPED"},
    )
    assert await store.get_audio_status(bvid) == "skipped"


async def test_save_audio_transcription_replaces_existing_row(
    store: ProcessingStore,
) -> None:
    bvid = "BVreplace"
    await _seed_video(store, bvid)
    # First write: failed.
    await store.save_audio_transcription(
        bvid,
        status="failed",
        transcription_source=None,
        transcript=None,
        audio_tokens=None,
        seconds=None,
        cache_hits=None,
        payload={"v": 1},
        processed_at_ms=100,
    )
    # Second write: success retry overwrites the failed row.
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript="redo",
        audio_tokens=10,
        seconds=1.0,
        cache_hits=0,
        payload={"v": 2},
        processed_at_ms=200,
    )

    rows = await store.ctx.conn.fetch_all(
        "SELECT status, transcript, payload, processed_at_ms "
        "FROM audio_transcription WHERE bvid = ?",
        (bvid,),
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["transcript"] == "redo"
    assert json.loads(rows[0]["payload"]) == {"v": 2}
    assert rows[0]["processed_at_ms"] == 200


async def test_save_audio_transcription_failed_clears_materialized_rows(
    store: ProcessingStore,
) -> None:
    bvid = "BVclear"
    await _seed_video(store, bvid)
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript="ok",
        audio_tokens=1,
        seconds=1.0,
        cache_hits=0,
        payload={
            "result": {
                "pages": [
                    {
                        "page_index": 0,
                        "text": "ok",
                        "segments": [{"start_s": 0, "end_s": 1, "text": "ok"}],
                    },
                ],
            },
        },
    )
    assert await store.ctx.conn.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription_page WHERE bvid = ?",
        (bvid,),
    ) == 1

    await store.save_audio_transcription(
        bvid,
        status="failed",
        transcription_source=None,
        transcript=None,
        audio_tokens=None,
        seconds=None,
        cache_hits=None,
        payload={"result": None},
    )

    assert await store.ctx.conn.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription_page WHERE bvid = ?",
        (bvid,),
    ) == 0
    assert await store.ctx.conn.fetch_value(
        "SELECT COUNT(*) FROM audio_transcription_segment WHERE bvid = ?",
        (bvid,),
    ) == 0


async def test_save_audio_transcription_non_success_statuses_clear_materialized_rows(
    store: ProcessingStore,
) -> None:
    for status in ("skipped", "pending", "running"):
        bvid = f"BVclear{status}"
        await _seed_video(store, bvid)
        await store.save_audio_transcription(
            bvid,
            status="success",
            transcription_source="MIMO-ASR",
            transcript="ok",
            audio_tokens=1,
            seconds=1.0,
            cache_hits=0,
            payload={
                "result": {
                    "pages": [{
                        "page_index": 0,
                        "text": "ok",
                        "segments": [{"start_s": 0, "end_s": 1, "text": "ok"}],
                    }],
                },
            },
        )
        await store.save_audio_transcription(
            bvid,
            status=status,
            transcription_source=None,
            transcript=None,
            audio_tokens=None,
            seconds=None,
            cache_hits=None,
            payload={"status": status.upper()},
        )

        assert await store.ctx.conn.fetch_value(
            "SELECT COUNT(*) FROM audio_transcription_page WHERE bvid = ?",
            (bvid,),
        ) == 0
        assert await store.ctx.conn.fetch_value(
            "SELECT COUNT(*) FROM audio_transcription_segment WHERE bvid = ?",
            (bvid,),
        ) == 0


async def test_save_audio_transcription_success_resave_rebuilds_materialized_rows(
    store: ProcessingStore,
) -> None:
    bvid = "BVresave"
    await _seed_video(store, bvid)
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript="one two",
        audio_tokens=2,
        seconds=2.0,
        cache_hits=0,
        payload={
            "result": {
                "pages": [{
                    "page_index": 0,
                    "text": "one two",
                    "segments": [
                        {"start_s": 0, "end_s": 1, "text": "one"},
                        {"start_s": 1, "end_s": 2, "text": "two"},
                    ],
                }],
            },
        },
    )
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript="three",
        audio_tokens=1,
        seconds=1.0,
        cache_hits=0,
        payload={
            "result": {
                "pages": [{
                    "page_index": 0,
                    "text": "three",
                    "segments": [{"start_s": 0, "end_s": 1, "text": "three"}],
                }],
            },
        },
    )

    rows = await store.ctx.conn.fetch_all(
        "SELECT segment_no, transcript_text "
        "FROM audio_transcription_segment WHERE bvid = ? ORDER BY segment_no",
        (bvid,),
    )
    assert [dict(r) for r in rows] == [
        {"segment_no": 1, "transcript_text": "three"},
    ]


# ---------------------------------------------------------------------------
# get_audio_status / get_audio_payload
# ---------------------------------------------------------------------------

async def test_get_audio_status_missing_returns_none(store: ProcessingStore) -> None:
    assert await store.get_audio_status("BVnope") is None


async def test_get_audio_payload_missing_returns_none(store: ProcessingStore) -> None:
    assert await store.get_audio_payload("BVnope") is None


async def test_get_audio_payload_round_trip(store: ProcessingStore) -> None:
    bvid = "BVround"
    await _seed_video(store, bvid)
    payload = {"deeply": {"nested": [1, 2, {"three": "三"}]}}
    await store.save_audio_transcription(
        bvid,
        status="success",
        transcription_source="MIMO-ASR",
        transcript=None,
        audio_tokens=None,
        seconds=None,
        cache_hits=None,
        payload=payload,
    )
    got = await store.get_audio_payload(bvid)
    assert got == payload


# ---------------------------------------------------------------------------
# list_audio_bvids / list_failed_audio_bvids
# ---------------------------------------------------------------------------

async def test_list_audio_bvids_filters_by_status(store: ProcessingStore) -> None:
    for bvid, status in [
        ("BVa", "success"),
        ("BVb", "success"),
        ("BVc", "failed"),
        ("BVd", "skipped"),
    ]:
        await _seed_video(store, bvid)
        await store.save_audio_transcription(
            bvid,
            status=status,
            transcription_source=None,
            transcript=None,
            audio_tokens=None,
            seconds=None,
            cache_hits=None,
            payload={"item_id": bvid},
        )
    assert await store.list_audio_bvids() == ["BVa", "BVb", "BVc", "BVd"]
    assert await store.list_audio_bvids(status="success") == ["BVa", "BVb"]
    assert await store.list_audio_bvids(status="failed") == ["BVc"]
    assert await store.list_audio_bvids(status="skipped") == ["BVd"]
    assert await store.list_audio_bvids(status="pending") == []


async def test_list_failed_audio_bvids(store: ProcessingStore) -> None:
    for bvid, status in [
        ("BVok1", "success"),
        ("BVfail1", "failed"),
        ("BVfail2", "failed"),
        ("BVskip", "skipped"),
    ]:
        await _seed_video(store, bvid)
        await store.save_audio_transcription(
            bvid,
            status=status,
            transcription_source=None,
            transcript=None,
            audio_tokens=None,
            seconds=None,
            cache_hits=None,
            payload={"item_id": bvid},
        )
    assert await store.list_failed_audio_bvids() == ["BVfail1", "BVfail2"]


async def test_list_audio_bvids_empty(store: ProcessingStore) -> None:
    assert await store.list_audio_bvids() == []
    assert await store.list_failed_audio_bvids() == []


# ---------------------------------------------------------------------------
# init_task / update_task_pipeline / update_task_status / get_task
# ---------------------------------------------------------------------------

async def test_init_task_creates_row_with_pending_pipelines(
    store: ProcessingStore,
) -> None:
    await store.init_task(["audio"])
    task = await store.get_task()
    assert task is not None
    assert task["status"] == "PENDING"
    assert task["payload"] == {
        "pipelines": {"audio": {"status": "PENDING", "items": {}}},
    }
    assert task["created_at_ms"] > 0
    assert task["updated_at_ms"] == task["created_at_ms"]


async def test_init_task_idempotent_preserves_existing_state(
    store: ProcessingStore,
) -> None:
    await store.init_task(["audio"])
    # Move audio out of PENDING.
    await store.update_task_pipeline(
        "audio",
        status="RUNNING",
        items={"transcription": {"total": 5, "completed": 1, "failed": 0, "skipped": 0}},
    )
    before = await store.get_task()
    assert before is not None
    audio_before = before["payload"]["pipelines"]["audio"]
    assert audio_before["status"] == "RUNNING"
    assert audio_before["items"]["transcription"]["completed"] == 1

    # Re-init with the same pipelines: must not reset audio's status / items.
    await store.init_task(["audio"])
    after = await store.get_task()
    assert after is not None
    assert after["payload"]["pipelines"]["audio"] == audio_before


async def test_init_task_adds_new_pipelines_without_overwriting(
    store: ProcessingStore,
) -> None:
    await store.init_task(["audio"])
    await store.update_task_pipeline("audio", status="SUCCESS")
    # Add a hypothetical second pipeline.
    await store.init_task(["audio", "subtitle"])
    task = await store.get_task()
    assert task is not None
    pipelines = task["payload"]["pipelines"]
    assert pipelines["audio"]["status"] == "SUCCESS"
    assert pipelines["subtitle"] == {"status": "PENDING", "items": {}}


async def test_update_task_pipeline_mutates_only_named(
    store: ProcessingStore,
) -> None:
    # Two pipelines side by side; mutate one and confirm the other is intact.
    await store.init_task(["audio", "subtitle"])
    await store.update_task_pipeline(
        "subtitle",
        status="SUCCESS",
        items={"shortcut": {"total": 7, "completed": 7, "failed": 0, "skipped": 0}},
    )
    await store.update_task_pipeline(
        "audio",
        status="RUNNING",
        items={"transcription": {"total": 3, "completed": 1, "failed": 0, "skipped": 0}},
    )

    task = await store.get_task()
    assert task is not None
    pipelines = task["payload"]["pipelines"]
    assert pipelines["audio"]["status"] == "RUNNING"
    assert pipelines["audio"]["items"] == {
        "transcription": {"total": 3, "completed": 1, "failed": 0, "skipped": 0},
    }
    # Subtitle row must still be intact after the audio update.
    assert pipelines["subtitle"]["status"] == "SUCCESS"
    assert pipelines["subtitle"]["items"] == {
        "shortcut": {"total": 7, "completed": 7, "failed": 0, "skipped": 0},
    }


async def test_update_task_pipeline_items_none_keeps_existing_items(
    store: ProcessingStore,
) -> None:
    await store.init_task(["audio"])
    await store.update_task_pipeline(
        "audio",
        status="RUNNING",
        items={"transcription": {"total": 2, "completed": 0, "failed": 0, "skipped": 0}},
    )
    # Update only the status; items must stay where they were.
    await store.update_task_pipeline("audio", status="SUCCESS")
    task = await store.get_task()
    assert task is not None
    audio = task["payload"]["pipelines"]["audio"]
    assert audio["status"] == "SUCCESS"
    assert audio["items"] == {
        "transcription": {"total": 2, "completed": 0, "failed": 0, "skipped": 0},
    }


async def test_update_task_pipeline_no_task_row_is_noop(
    store: ProcessingStore,
) -> None:
    # init_task was never called -- update_task_pipeline must not crash.
    await store.update_task_pipeline("audio", status="RUNNING")
    assert await store.get_task() is None


async def test_update_task_status(store: ProcessingStore) -> None:
    await store.init_task(["audio"])
    await store.update_task_status("RUNNING")
    task = await store.get_task()
    assert task is not None
    assert task["status"] == "RUNNING"
    await store.update_task_status("SUCCESS")
    task2 = await store.get_task()
    assert task2 is not None
    assert task2["status"] == "SUCCESS"


async def test_get_task_missing_returns_none(store: ProcessingStore) -> None:
    assert await store.get_task() is None


# ---------------------------------------------------------------------------
# record_error / list_errors
# ---------------------------------------------------------------------------

async def test_record_error_returns_monotonic_ids(store: ProcessingStore) -> None:
    id1 = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="DownloadError", message="cdn 503",
        retryable=True, detail={"retry_count": 0}, occurred_at_ms=1,
    )
    id2 = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="DownloadError", message="cdn 504",
        retryable=True, detail={"retry_count": 1}, occurred_at_ms=2,
    )
    id3 = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV2",
        error_type="ASRConfigError", message="bad key",
        retryable=False,
    )
    assert id1 < id2 < id3
    assert id2 == id1 + 1
    assert id3 == id2 + 1


async def test_record_error_persists_columns(store: ProcessingStore) -> None:
    eid = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="DownloadError", message="boom",
        retryable=True, detail={"k": "v"}, occurred_at_ms=99,
    )
    row = await store.ctx.conn.fetch_one(
        "SELECT stage, pipeline, item_type, item_id, error_type, message, "
        "       retryable, detail, occurred_at_ms "
        "FROM stage_error WHERE id = ?",
        (eid,),
    )
    assert row is not None
    assert row["stage"] == "asr"
    assert row["pipeline"] == "audio"
    assert row["item_type"] == "transcription"
    assert row["item_id"] == "BV1"
    assert row["error_type"] == "DownloadError"
    assert row["message"] == "boom"
    assert row["retryable"] == 1
    assert json.loads(row["detail"]) == {"k": "v"}
    assert row["occurred_at_ms"] == 99


async def test_record_error_retryable_tristate(store: ProcessingStore) -> None:
    id_true = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="y", retryable=True,
    )
    id_false = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="y", retryable=False,
    )
    id_unknown = await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="y", retryable=None,
    )
    rows = {
        r["id"]: r for r in await store.ctx.conn.fetch_all(
            "SELECT id, retryable FROM stage_error",
        )
    }
    assert rows[id_true]["retryable"] == 1
    assert rows[id_false]["retryable"] == 0
    assert rows[id_unknown]["retryable"] is None


async def test_list_errors_filters_by_pipeline(store: ProcessingStore) -> None:
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="a", retryable=True,
    )
    await store.record_error(
        pipeline="subtitle", item_type="page", item_id="BV1",
        error_type="Y", message="b", retryable=False,
    )
    audio_errors = await store.list_errors(pipeline="audio")
    sub_errors = await store.list_errors(pipeline="subtitle")
    assert len(audio_errors) == 1
    assert audio_errors[0]["pipeline"] == "audio"
    assert audio_errors[0]["error_type"] == "X"
    assert audio_errors[0]["retryable"] is True
    assert len(sub_errors) == 1
    assert sub_errors[0]["pipeline"] == "subtitle"
    assert sub_errors[0]["retryable"] is False


async def test_list_errors_filters_by_item_id(store: ProcessingStore) -> None:
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV_A",
        error_type="X", message="a", retryable=True,
    )
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV_B",
        error_type="X", message="b", retryable=True,
    )
    rows = await store.list_errors(item_id="BV_A")
    assert len(rows) == 1
    assert rows[0]["item_id"] == "BV_A"


async def test_list_errors_filters_by_item_type(store: ProcessingStore) -> None:
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="a", retryable=True,
    )
    await store.record_error(
        pipeline="audio", item_type="frame", item_id="BV1",
        error_type="X", message="b", retryable=True,
    )
    trans = await store.list_errors(item_type="transcription")
    assert len(trans) == 1
    assert trans[0]["item_type"] == "transcription"


async def test_list_errors_returns_newest_first(store: ProcessingStore) -> None:
    eids = []
    for i in range(3):
        eids.append(await store.record_error(
            pipeline="audio", item_type="transcription", item_id=f"BV{i}",
            error_type="X", message=f"m{i}", retryable=None,
        ))
    rows = await store.list_errors()
    assert [r["id"] for r in rows] == list(reversed(eids))


async def test_list_errors_decodes_detail_and_retryable(
    store: ProcessingStore,
) -> None:
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="m",
        retryable=None, detail={"k": [1, 2]},
    )
    rows = await store.list_errors()
    assert len(rows) == 1
    assert rows[0]["retryable"] is None
    assert rows[0]["detail"] == {"k": [1, 2]}


async def test_list_errors_empty(store: ProcessingStore) -> None:
    assert await store.list_errors() == []
    assert await store.list_errors(pipeline="audio") == []


async def test_list_errors_does_not_leak_other_stage_rows(
    store: ProcessingStore,
) -> None:
    # Drop a fetching-stage row directly; it must not surface from list_errors.
    await store.ctx.conn.execute(
        "INSERT INTO stage_error("
        "    stage, endpoint, error_type, message, retryable, occurred_at_ms"
        ") VALUES ('fetching', 'user_info', 'NetError', 'boom', 1, 1)",
    )
    await store.record_error(
        pipeline="audio", item_type="transcription", item_id="BV1",
        error_type="X", message="m", retryable=True,
    )
    rows = await store.list_errors()
    assert len(rows) == 1
    assert rows[0]["stage"] == "asr"
