from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bili_unit._db import UidContext
from bili_unit.observability import load_dashboard_snapshot, load_uid_dashboard_snapshot


async def _seed_dashboard_uid(root: Path, uid: int = 123) -> None:
    """Populate raw_payload + audio_transcription + stage_run for ``uid``."""
    ctx = UidContext(uid=uid, root=root)
    await ctx.open()
    try:
        await ctx.conn.execute(
            "INSERT INTO raw_payload(endpoint, item_id, payload, fetched_at_ms) VALUES (?, ?, ?, ?)",
            ("video_detail", "BV1", json.dumps({"info": {"bvid": "BV1"}}), 1),
        )
        await ctx.conn.execute(
            "INSERT INTO audio_transcription("
            "    bvid, status, transcription_source, transcript, audio_tokens, "
            "    seconds, cache_hits, payload, processed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("BV1", "success", "mock", "text", 7, 3.5, 1, "{}", 2),
        )
        await ctx.conn.execute(
            "INSERT INTO stage_run("
            "    run_id, uid, command, status, started_at_ms, ended_at_ms, "
            "    args_json, summary_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                uid,
                "asr",
                "SUCCESS",
                100,
                200,
                json.dumps({"mode": "incremental"}),
                "{}",
            ),
        )
    finally:
        await ctx.close()


async def test_uid_dashboard_snapshot_reads_manifest_and_latest_run(tmp_path: Path) -> None:
    await _seed_dashboard_uid(tmp_path, uid=123)

    snapshot = await load_uid_dashboard_snapshot(uid=123, root=tmp_path)

    assert snapshot.available is True
    assert snapshot.read_error is None
    assert snapshot.manifest is not None
    assert snapshot.manifest.uid == 123
    assert snapshot.manifest.schema_version == 3
    assert snapshot.manifest.video_count == 1
    assert snapshot.manifest.transcribed_count == 1
    assert snapshot.manifest.total_audio_tokens == 7
    assert snapshot.manifest.total_audio_seconds == 3.5
    assert snapshot.run_summary is not None
    assert snapshot.run_summary.run is not None
    assert snapshot.run_summary.run.run_id == "run-1"
    assert snapshot.active is False
    assert snapshot.active_stages == ()
    assert snapshot.recommended_actions == []


async def test_uid_dashboard_snapshot_recommends_asr_next_actions(tmp_path: Path) -> None:
    uid = 123
    await _seed_dashboard_uid(tmp_path, uid=uid)
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    try:
        for bvid, status in (("BVfailed", "failed"), ("BVmissing", None)):
            await ctx.conn.execute(
                "INSERT INTO raw_payload(endpoint, item_id, payload, fetched_at_ms) VALUES (?, ?, ?, ?)",
                ("video_detail", bvid, json.dumps({"info": {"bvid": bvid}}), 1),
            )
            if status is not None:
                await ctx.conn.execute(
                    "INSERT INTO audio_transcription("
                    "    bvid, status, transcription_source, transcript, "
                    "    audio_tokens, seconds, cache_hits, payload, processed_at_ms"
                    ") VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, '{}', 2)",
                    (bvid, status),
                )
    finally:
        await ctx.close()

    snapshot = await load_uid_dashboard_snapshot(uid=uid, root=tmp_path)

    assert [(action.kind, action.item_ids) for action in snapshot.recommended_actions] == [
        ("asr_retry_failed", ("BVfailed",)),
        ("asr_run_missing", ("BVmissing",)),
    ]
    assert snapshot.recommended_actions[0].command == ("uv run bili-unit asr 123 --retry-failed-only")
    assert snapshot.recommended_actions[1].command == ("uv run bili-unit asr 123 --only-bvids BVmissing")


async def test_dashboard_snapshot_lists_uids(tmp_path: Path) -> None:
    await _seed_dashboard_uid(tmp_path, uid=123)
    await _seed_dashboard_uid(tmp_path, uid=456)

    snapshot = await load_dashboard_snapshot(root=tmp_path)

    assert snapshot.uids == [123, 456]
    assert [item.uid for item in snapshot.items] == [123, 456]


async def test_dashboard_snapshot_handles_missing_uid(tmp_path: Path) -> None:
    snapshot = await load_uid_dashboard_snapshot(uid=999, root=tmp_path)

    assert snapshot.available is False
    assert snapshot.active is False
    assert snapshot.active_stages == ()
    assert snapshot.manifest is None
    assert snapshot.run_summary is None
    assert snapshot.read_error == "DB does not exist"


async def test_uid_dashboard_snapshot_exposes_active_stages(tmp_path: Path) -> None:
    uid = 123
    await _seed_dashboard_uid(tmp_path, uid=uid)
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    try:
        await ctx.conn.execute(
            "INSERT INTO stage_task(    stage, status, payload, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
            ("fetching", "RUNNING", '{"endpoints":[]}', 1, 2),
        )
    finally:
        await ctx.close()

    snapshot = await load_uid_dashboard_snapshot(uid=uid, root=tmp_path)

    assert snapshot.active is True
    assert snapshot.active_stages == ("fetching",)


async def test_dashboard_snapshot_can_read_while_writer_updates(tmp_path: Path) -> None:
    uid = 123
    await _seed_dashboard_uid(tmp_path, uid=uid)
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    try:

        async def writer() -> None:
            for idx in range(20):
                await ctx.conn.execute(
                    "INSERT INTO stage_event("
                    "    run_id, ts_ms, level, stage, event, endpoint, pipeline, "
                    "    item_type, item_id, message, data_json"
                    ") VALUES (?, ?, 'INFO', 'asr', 'asr.item.completed', "
                    "NULL, 'audio', 'transcription', ?, NULL, '{}')",
                    ("run-1", 300 + idx, f"BV{idx}"),
                )
                await asyncio.sleep(0)

        async def reader() -> None:
            for _ in range(20):
                snapshot = await load_uid_dashboard_snapshot(uid=uid, root=tmp_path)
                assert snapshot.available is True
                assert snapshot.manifest is not None
                await asyncio.sleep(0)

        await asyncio.gather(writer(), reader())
    finally:
        await ctx.close()
