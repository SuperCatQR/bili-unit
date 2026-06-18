from __future__ import annotations

import json
import logging
from pathlib import Path

from bili_unit._db import UidContext
from bili_unit.observability import (
    CompositeSink,
    JsonlSink,
    LoggingSink,
    MemorySink,
    RunContext,
    RunReporter,
    SqliteSink,
    build_run_summary,
    load_run_summary,
)


def test_run_context_create_defaults() -> None:
    ctx = RunContext.create(uid=123, command="asr", args={"mode": "incremental"})
    assert ctx.uid == 123
    assert ctx.command == "asr"
    assert ctx.args == {"mode": "incremental"}
    assert len(ctx.run_id) == 32
    assert ctx.started_at_ms > 0


async def test_reporter_auto_starts_and_records_event() -> None:
    sink = MemorySink()
    ctx = RunContext.create(
        uid=42,
        command="asr",
        args={"limit": 1},
        run_id="run-1",
        started_at_ms=100,
    )
    reporter = RunReporter(ctx, sink)

    event = await reporter.emit(
        "asr.item.retry_scheduled",
        stage="asr",
        level="WARNING",
        pipeline="audio",
        item_type="transcription",
        item_id="BV1",
        message="retry later",
        data={"delay_s": 60},
    )
    await reporter.complete("PARTIAL", summary={"success": 1, "failed": 1})

    assert sink.started == [ctx]
    assert sink.events == [event]
    assert event.to_dict() == {
        "run_id": "run-1",
        "uid": 42,
        "stage": "asr",
        "event": "asr.item.retry_scheduled",
        "level": "WARNING",
        "ts_ms": event.ts_ms,
        "endpoint": None,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": "BV1",
        "message": "retry later",
        "data": {"delay_s": 60},
    }
    assert sink.completed == [(ctx, "PARTIAL", {"success": 1, "failed": 1})]


async def test_composite_sink_fans_out() -> None:
    sink_a = MemorySink()
    sink_b = MemorySink()
    ctx = RunContext.create(uid=1, command="fetch", run_id="run-2")
    reporter = RunReporter(ctx, CompositeSink([sink_a, sink_b]))

    await reporter.emit("fetch.endpoint.started", stage="fetching", endpoint="videos")
    await reporter.complete("SUCCESS")

    assert [s.started for s in (sink_a, sink_b)] == [[ctx], [ctx]]
    assert [s.events[0].event for s in (sink_a, sink_b)] == [
        "fetch.endpoint.started",
        "fetch.endpoint.started",
    ]
    assert [s.completed[0][1] for s in (sink_a, sink_b)] == ["SUCCESS", "SUCCESS"]


async def test_jsonl_sink_writes_run_and_event_records(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "run.jsonl"
    ctx = RunContext.create(
        uid=7,
        command="asr",
        args={"retry_failed_only": True},
        run_id="run-jsonl",
        started_at_ms=1234,
    )
    reporter = RunReporter(ctx, JsonlSink(path))

    await reporter.emit(
        "asr.coverage.partial",
        stage="asr",
        data={"missing": 2},
    )
    await reporter.complete("PARTIAL", summary={"missing": 2})

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {
        "type": "run.started",
        "run_id": "run-jsonl",
        "uid": 7,
        "command": "asr",
        "args": {"retry_failed_only": True},
        "started_at_ms": 1234,
    }
    assert lines[1]["type"] == "event"
    assert lines[1]["event"] == "asr.coverage.partial"
    assert lines[1]["data"] == {"missing": 2}
    assert lines[2]["type"] == "run.completed"
    assert lines[2]["status"] == "PARTIAL"
    assert lines[2]["summary"] == {"missing": 2}
    assert lines[2]["ended_at_ms"] > 0


async def test_logging_sink_emits_structured_records(caplog) -> None:
    logger = logging.getLogger("test.observability")
    ctx = RunContext.create(uid=9, command="parse", run_id="run-log")
    reporter = RunReporter(ctx, LoggingSink(logger))

    with caplog.at_level(logging.INFO, logger="test.observability"):
        await reporter.emit(
            "parse.model.failed",
            stage="parsing",
            level="ERROR",
            item_type="model",
            item_id="article",
            message="model failed",
        )
        await reporter.complete("PARTIAL")

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["run.started", "model failed", "run.completed"]
    event_record = caplog.records[1]
    assert event_record.run_id == "run-log"
    assert event_record.uid == 9
    assert event_record.stage == "parsing"
    assert event_record.event == "parse.model.failed"
    assert event_record.item_id == "article"


async def test_sqlite_sink_persists_run_and_events(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=55, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        run_ctx = RunContext.create(
            uid=55,
            command="fetch",
            args={"profile": "parsing"},
            run_id="run-sqlite",
            started_at_ms=2000,
        )
        reporter = RunReporter(run_ctx, SqliteSink(ctx_db.main))

        await reporter.emit(
            "fetch.endpoint.completed",
            stage="fetching",
            endpoint="videos",
            data={"completed": 1},
        )
        await reporter.emit(
            "fetch.item.unavailable",
            stage="fetching",
            level="WARNING",
            endpoint="article_detail",
            item_type="cvid",
            item_id="123",
            message="article unavailable",
        )
        await reporter.complete("PARTIAL", summary={"endpoints": 2})

        run_row = await ctx_db.main.fetch_one(
            "SELECT uid, command, status, started_at_ms, ended_at_ms, "
            "       args_json, summary_json "
            "FROM stage_run WHERE run_id = ?",
            ("run-sqlite",),
        )
        assert run_row is not None
        assert run_row["uid"] == 55
        assert run_row["command"] == "fetch"
        assert run_row["status"] == "PARTIAL"
        assert run_row["started_at_ms"] == 2000
        assert run_row["ended_at_ms"] > 0
        assert json.loads(run_row["args_json"]) == {"profile": "parsing"}
        assert json.loads(run_row["summary_json"]) == {"endpoints": 2}

        rows = await ctx_db.main.fetch_all(
            "SELECT level, stage, event, endpoint, item_type, item_id, "
            "       message, data_json "
            "FROM stage_event WHERE run_id = ? ORDER BY id",
            ("run-sqlite",),
        )
        assert len(rows) == 2
        assert rows[0]["level"] == "INFO"
        assert rows[0]["stage"] == "fetching"
        assert rows[0]["event"] == "fetch.endpoint.completed"
        assert rows[0]["endpoint"] == "videos"
        assert json.loads(rows[0]["data_json"]) == {"completed": 1}
        assert rows[1]["level"] == "WARNING"
        assert rows[1]["event"] == "fetch.item.unavailable"
        assert rows[1]["item_type"] == "cvid"
        assert rows[1]["item_id"] == "123"
        assert rows[1]["message"] == "article unavailable"
        assert json.loads(rows[1]["data_json"]) == {}
    finally:
        await ctx_db.close()


async def test_build_run_summary_reads_current_state_and_latest_run(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=66, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        await _seed_summary_state(ctx_db)

        summary = await build_run_summary(ctx_db.main, uid=66, recent_limit=10)

        assert summary.uid == 66
        assert summary.run is not None
        assert summary.run.run_id == "run-new"
        assert summary.run.command == "asr"
        assert summary.run.status == "PARTIAL"
        assert summary.run.args == {"mode": "incremental"}
        assert summary.fetch.status == "PARTIAL"
        assert summary.fetch.status_counts == {"SUCCESS": 1, "FAILED_EXHAUSTED": 1}
        assert [endpoint.endpoint for endpoint in summary.fetch.endpoints] == [
            "article_detail",
            "videos",
        ]
        assert summary.fetch.endpoints[0].item_progress == {"done": 1, "total": 2}

        assert summary.parse.status == "PARTIAL"
        assert summary.parse.status_counts == {"FAILED": 1, "SUCCESS": 1}
        assert [(m.model, m.status, m.count) for m in summary.parse.models] == [
            ("article_post", "FAILED", 1),
            ("video_work", "SUCCESS", 3),
        ]
        assert summary.parse.images == {"total": 2, "ok": 1, "failed": 1}

        assert summary.asr.status == "PARTIAL"
        assert summary.asr.candidate_count == 3
        assert summary.asr.expected == 3
        assert summary.asr.success == 1
        assert summary.asr.failed == 1
        assert summary.asr.missing == 1
        assert summary.asr.complete is False
        assert summary.asr.failed_bvids == ["BV2"]
        assert summary.asr.missing_bvids == ["BV3"]
        assert summary.asr.status_counts == {"failed": 1, "success": 1}

        assert [event.event for event in summary.recent_events] == [
            "asr.discovery.completed",
            "asr.item.retry_scheduled",
            "asr.segment.high_risk_split",
            "asr.coverage.partial",
        ]
        assert [event.event for event in summary.recent_attention_events] == [
            "asr.item.retry_scheduled",
            "asr.segment.high_risk_split",
            "asr.coverage.partial",
        ]
    finally:
        await ctx_db.close()


async def test_build_run_summary_can_select_specific_run(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=66, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        await _seed_summary_state(ctx_db)

        summary = await build_run_summary(ctx_db.main, uid=66, run_id="run-old")

        assert summary.run is not None
        assert summary.run.run_id == "run-old"
        assert summary.run.command == "fetch"
        assert [event.event for event in summary.recent_events] == [
            "fetch.endpoint.completed",
        ]
    finally:
        await ctx_db.close()


async def test_run_summary_candidate_count_ignores_recent_event_limit(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=66, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        await _seed_summary_state(ctx_db)

        summary = await build_run_summary(ctx_db.main, uid=66, run_id="run-new", recent_limit=1)

        assert summary.asr.candidate_count == 3
        assert [event.event for event in summary.recent_events] == [
            "asr.coverage.partial",
        ]
    finally:
        await ctx_db.close()


async def test_run_summary_reads_current_state_without_run_records(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=77, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        await ctx_db.main.execute(
            "INSERT INTO stage_task(stage, status, payload, created_at_ms, updated_at_ms) "
            "VALUES ('asr', 'PARTIAL', ?, 1, 2)",
            (
                json.dumps({
                    "pipelines": {
                        "audio": {"status": "PARTIAL", "items": {}},
                    },
                }),
            ),
        )
        for bvid in ("BVok", "BVmissing"):
            await ctx_db.main.execute(
                "INSERT INTO video("
                "    bvid, aid, title, description, cover_url, duration_s, "
                "    pubdate_ms, view_count, danmaku, reply, favorite, coin, "
                "    share, like_count, payload, parsed_at_ms"
                ") VALUES (?, NULL, ?, '', '', 0, NULL, 0, 0, 0, 0, 0, 0, 0, '{}', 1)",
                (bvid, bvid),
            )
        await ctx_db.main.execute(
            "INSERT INTO audio_transcription("
            "    bvid, status, transcription_source, transcript, audio_tokens, "
            "    seconds, cache_hits, payload, processed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("BVok", "success", "mimo", "ok", 1, 1.0, 0, "{}", 2),
        )

        summary = await build_run_summary(ctx_db.main, uid=77)

        assert summary.run is None
        assert summary.recent_events == []
        assert summary.asr.status == "PARTIAL"
        assert summary.asr.expected == 2
        assert summary.asr.success == 1
        assert summary.asr.missing == 1
        assert summary.asr.missing_bvids == ["BVmissing"]
    finally:
        await ctx_db.close()


async def test_load_run_summary_opens_main_db(tmp_path: Path) -> None:
    ctx_db = UidContext(uid=66, root=tmp_path)
    await ctx_db.open(raw=False)
    try:
        await _seed_summary_state(ctx_db)
    finally:
        await ctx_db.close()

    summary = await load_run_summary(uid=66, root=tmp_path, run_id="run-new")

    assert summary.run is not None
    assert summary.run.run_id == "run-new"
    assert summary.asr.expected == 3


async def _seed_summary_state(ctx_db: UidContext) -> None:
    await ctx_db.main.execute(
        "INSERT INTO stage_run("
        "    run_id, uid, command, status, started_at_ms, ended_at_ms, "
        "    args_json, summary_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-old",
            66,
            "fetch",
            "SUCCESS",
            1000,
            1500,
            json.dumps({"profile": "base"}),
            json.dumps({"endpoints": 1}),
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO stage_run("
        "    run_id, uid, command, status, started_at_ms, ended_at_ms, "
        "    args_json, summary_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-new",
            66,
            "asr",
            "PARTIAL",
            2000,
            2500,
            json.dumps({"mode": "incremental"}),
            json.dumps({"status": "PARTIAL"}),
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO stage_event("
        "    run_id, ts_ms, level, stage, event, endpoint, pipeline, "
        "    item_type, item_id, message, data_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-old",
            1100,
            "INFO",
            "fetching",
            "fetch.endpoint.completed",
            "videos",
            None,
            None,
            None,
            None,
            json.dumps({"count": 3}),
        ),
    )
    for idx, (event, level, data) in enumerate(
        [
            ("asr.discovery.completed", "INFO", {"candidate_count": 3}),
            ("asr.item.retry_scheduled", "INFO", {"retry": 1}),
            ("asr.segment.high_risk_split", "INFO", {"pieces": 2}),
            ("asr.coverage.partial", "WARNING", {"missing": 1, "failed": 1}),
        ],
        start=1,
    ):
        await ctx_db.main.execute(
            "INSERT INTO stage_event("
            "    run_id, ts_ms, level, stage, event, endpoint, pipeline, "
            "    item_type, item_id, message, data_json"
            ") VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?)",
            (
                "run-new",
                2000 + idx,
                level,
                "asr",
                event,
                "audio",
                "transcription",
                "BV2" if "item" in event else None,
                json.dumps(data),
            ),
        )
    await ctx_db.main.execute(
        "INSERT INTO stage_task(stage, status, payload, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "fetching",
            "PARTIAL",
            json.dumps({"endpoints": ["videos", "article_detail"]}),
            1,
            2,
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO stage_task(stage, status, payload, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "parsing",
            "PARTIAL",
            json.dumps({
                "models": {
                    "video_work": {"status": "SUCCESS", "count": 3},
                    "article_post": {"status": "FAILED", "count": 1},
                },
                "images": {"total": 2, "ok": 1, "failed": 1},
            }),
            1,
            2,
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO stage_task(stage, status, payload, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "asr",
            "PARTIAL",
            json.dumps({
                "pipelines": {
                    "audio": {
                        "status": "PARTIAL",
                        "items": {},
                    },
                },
            }),
            1,
            2,
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO fetch_endpoint_state("
        "    endpoint, status, retry_count, last_error_id, "
        "    item_progress, progress, updated_at_ms"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "videos",
            "SUCCESS",
            0,
            None,
            None,
            json.dumps({"fetched": 3, "total": 3}),
            2,
        ),
    )
    await ctx_db.main.execute(
        "INSERT INTO fetch_endpoint_state("
        "    endpoint, status, retry_count, last_error_id, "
        "    item_progress, progress, updated_at_ms"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "article_detail",
            "FAILED_EXHAUSTED",
            2,
            9,
            json.dumps({"done": 1, "total": 2}),
            None,
            3,
        ),
    )
    for bvid in ("BV1", "BV2", "BV3"):
        await ctx_db.main.execute(
            "INSERT INTO video("
            "    bvid, aid, title, description, cover_url, duration_s, "
            "    pubdate_ms, view_count, danmaku, reply, favorite, coin, "
            "    share, like_count, payload, parsed_at_ms"
            ") VALUES (?, NULL, ?, '', '', 0, NULL, 0, 0, 0, 0, 0, 0, 0, '{}', 1)",
            (bvid, bvid),
        )
    await ctx_db.main.execute(
        "INSERT INTO audio_transcription("
        "    bvid, status, transcription_source, transcript, audio_tokens, "
        "    seconds, cache_hits, payload, processed_at_ms"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("BV1", "success", "mimo", "ok", 10, 1.0, 0, "{}", 2),
    )
    await ctx_db.main.execute(
        "INSERT INTO audio_transcription("
        "    bvid, status, transcription_source, transcript, audio_tokens, "
        "    seconds, cache_hits, payload, processed_at_ms"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("BV2", "failed", None, None, None, None, None, "{}", 2),
    )
