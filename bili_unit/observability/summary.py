from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._db import Connection, UidContext


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    uid: int
    command: str
    status: str
    started_at_ms: int
    ended_at_ms: int | None
    args: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageTaskSummary:
    stage: str
    status: str
    payload: dict[str, Any]
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class FetchEndpointSummary:
    endpoint: str
    status: str
    retry_count: int
    last_error_id: int | None
    item_progress: dict[str, Any] | None
    progress: dict[str, Any] | None
    updated_at_ms: int


@dataclass(frozen=True)
class FetchSummary:
    status: str | None = None
    endpoints: list[FetchEndpointSummary] = field(default_factory=list)

    @property
    def status_counts(self) -> dict[str, int]:
        return _count_by_status(self.endpoints)


@dataclass(frozen=True)
class ParseModelSummary:
    model: str
    status: str
    count: int


@dataclass(frozen=True)
class ParseSummary:
    status: str | None = None
    models: list[ParseModelSummary] = field(default_factory=list)
    images: dict[str, Any] | None = None

    @property
    def status_counts(self) -> dict[str, int]:
        return _count_by_status(self.models)


@dataclass(frozen=True)
class AsrSummary:
    status: str | None = None
    coverage_applicable: bool = True
    candidate_count: int | None = None
    expected: int = 0
    success: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    pending: int = 0
    running: int = 0
    missing_bvids: list[str] = field(default_factory=list)
    failed_bvids: list[str] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.success == self.expected


@dataclass(frozen=True)
class RunEventSummary:
    id: int
    ts_ms: int
    level: str
    stage: str
    event: str
    endpoint: str | None
    pipeline: str | None
    item_type: str | None
    item_id: str | None
    message: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class RunSummary:
    uid: int
    run: RunRecord | None
    stage_tasks: dict[str, StageTaskSummary]
    fetch: FetchSummary
    parse: ParseSummary
    asr: AsrSummary
    recent_events: list[RunEventSummary]
    recent_attention_events: list[RunEventSummary]


async def load_run_summary(
    *,
    uid: int,
    root: str | Path,
    run_id: str | None = None,
    recent_limit: int = 20,
    filter_events_to_run: bool = True,
) -> RunSummary:
    """Open the uid main DB and build a read-only run summary."""
    ctx = UidContext(uid, root)
    await ctx.open(raw=False)
    try:
        return await build_run_summary(
            ctx.main,
            uid=uid,
            run_id=run_id,
            recent_limit=recent_limit,
            filter_events_to_run=filter_events_to_run,
        )
    finally:
        await ctx.close()


async def build_run_summary(
    main: Connection,
    *,
    uid: int,
    run_id: str | None = None,
    recent_limit: int = 20,
    filter_events_to_run: bool = True,
) -> RunSummary:
    """Read current run/state facts from a main DB connection."""
    run = await _load_run(main, uid=uid, run_id=run_id)
    selected_run_id = run.run_id if run is not None else None
    event_run_id = (
        selected_run_id if filter_events_to_run and selected_run_id is not None
        else run_id if filter_events_to_run
        else None
    )
    stage_tasks = await _load_stage_tasks(main)
    events = await _load_recent_events(
        main,
        uid=uid,
        run_id=event_run_id,
        limit=recent_limit,
    )
    attention_events = await _load_attention_events(
        main,
        uid=uid,
        run_id=event_run_id,
        limit=recent_limit,
    )
    return RunSummary(
        uid=uid,
        run=run,
        stage_tasks=stage_tasks,
        fetch=await _load_fetch_summary(main, stage_tasks),
        parse=_load_parse_summary(stage_tasks),
        asr=await _load_asr_summary(
            main,
            uid=uid,
            run_id=event_run_id,
            run=run,
            stage_tasks=stage_tasks,
        ),
        recent_events=events,
        recent_attention_events=attention_events,
    )


async def _load_run(
    main: Connection,
    *,
    uid: int,
    run_id: str | None,
) -> RunRecord | None:
    if run_id is None:
        row = await main.fetch_one(
            "SELECT run_id, uid, command, status, started_at_ms, ended_at_ms, "
            "       args_json, summary_json "
            "FROM stage_run WHERE uid = ? "
            "ORDER BY started_at_ms DESC, rowid DESC LIMIT 1",
            (uid,),
        )
    else:
        row = await main.fetch_one(
            "SELECT run_id, uid, command, status, started_at_ms, ended_at_ms, "
            "       args_json, summary_json "
            "FROM stage_run WHERE run_id = ?",
            (run_id,),
        )
    if row is None:
        return None
    return RunRecord(
        run_id=str(row["run_id"]),
        uid=int(row["uid"]),
        command=str(row["command"]),
        status=str(row["status"]),
        started_at_ms=int(row["started_at_ms"]),
        ended_at_ms=row["ended_at_ms"],
        args=_load_json_dict(row["args_json"]),
        summary=_load_json_dict(row["summary_json"]),
    )


async def _load_stage_tasks(main: Connection) -> dict[str, StageTaskSummary]:
    rows = await main.fetch_all(
        "SELECT stage, status, payload, created_at_ms, updated_at_ms "
        "FROM stage_task ORDER BY stage",
    )
    return {
        str(row["stage"]): StageTaskSummary(
            stage=str(row["stage"]),
            status=str(row["status"]),
            payload=_load_json_dict(row["payload"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )
        for row in rows
    }


async def _load_fetch_summary(
    main: Connection,
    stage_tasks: dict[str, StageTaskSummary],
) -> FetchSummary:
    rows = await main.fetch_all(
        "SELECT endpoint, status, retry_count, last_error_id, "
        "       item_progress, progress, updated_at_ms "
        "FROM fetch_endpoint_state ORDER BY endpoint",
    )
    endpoints = [
        FetchEndpointSummary(
            endpoint=str(row["endpoint"]),
            status=str(row["status"]),
            retry_count=int(row["retry_count"]),
            last_error_id=row["last_error_id"],
            item_progress=_load_optional_json_dict(row["item_progress"]),
            progress=_load_optional_json_dict(row["progress"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )
        for row in rows
    ]
    task = stage_tasks.get("fetching")
    return FetchSummary(
        status=task.status if task is not None else None,
        endpoints=endpoints,
    )


def _load_parse_summary(
    stage_tasks: dict[str, StageTaskSummary],
) -> ParseSummary:
    task = stage_tasks.get("parsing")
    if task is None:
        return ParseSummary()
    raw_models = task.payload.get("models")
    models: list[ParseModelSummary] = []
    if isinstance(raw_models, dict):
        for name, raw_entry in sorted(raw_models.items()):
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            models.append(
                ParseModelSummary(
                    model=str(name),
                    status=str(entry.get("status", "UNKNOWN")),
                    count=int(entry.get("count", 0) or 0),
                ),
            )
    images = task.payload.get("images")
    return ParseSummary(
        status=task.status,
        models=models,
        images=images if isinstance(images, dict) else None,
    )


async def _load_asr_summary(
    main: Connection,
    *,
    uid: int,
    run_id: str | None,
    run: RunRecord | None,
    stage_tasks: dict[str, StageTaskSummary],
) -> AsrSummary:
    task = stage_tasks.get("asr")
    audio_status = None
    if task is not None:
        pipelines = task.payload.get("pipelines")
        if isinstance(pipelines, dict):
            audio = pipelines.get("audio")
            if isinstance(audio, dict):
                audio_status = audio.get("status")

    video_rows = await main.fetch_all("SELECT bvid FROM video ORDER BY bvid")
    expected_bvids = [str(row["bvid"]) for row in video_rows]
    rows = await main.fetch_all(
        "SELECT bvid, status FROM audio_transcription ORDER BY bvid",
    )
    statuses = {str(row["bvid"]): str(row["status"]) for row in rows}
    status_counts: dict[str, int] = {}
    for status in statuses.values():
        status_counts[status] = status_counts.get(status, 0) + 1

    missing_bvids = [bvid for bvid in expected_bvids if bvid not in statuses]
    failed_bvids = [
        bvid for bvid in expected_bvids if statuses.get(bvid) == "failed"
    ]
    candidate_count = await _load_asr_candidate_count(
        main,
        uid=uid,
        run_id=run_id,
    )
    return AsrSummary(
        status=str(audio_status) if audio_status is not None else None,
        coverage_applicable=_asr_coverage_applicable(run),
        candidate_count=candidate_count,
        expected=len(expected_bvids),
        success=sum(1 for bvid in expected_bvids if statuses.get(bvid) == "success"),
        missing=len(missing_bvids),
        failed=len(failed_bvids),
        skipped=sum(1 for bvid in expected_bvids if statuses.get(bvid) == "skipped"),
        pending=sum(1 for bvid in expected_bvids if statuses.get(bvid) == "pending"),
        running=sum(1 for bvid in expected_bvids if statuses.get(bvid) == "running"),
        missing_bvids=missing_bvids,
        failed_bvids=failed_bvids,
        status_counts=status_counts,
    )


def _asr_coverage_applicable(run: RunRecord | None) -> bool:
    if run is None or run.command != "asr":
        return True
    args = run.args
    if args.get("dry_run"):
        return False
    if args.get("limit") is not None:
        return False
    if args.get("only_bvids") is not None:
        return False
    if args.get("exclude_bvids") is not None:
        return False
    if args.get("retry_failed_only"):
        return False
    return not bool(run.summary.get("budget_exceeded"))


async def _load_asr_candidate_count(
    main: Connection,
    *,
    uid: int,
    run_id: str | None,
) -> int | None:
    where, params = _event_where(uid=uid, run_id=run_id)
    row = await main.fetch_one(
        "SELECT e.data_json "
        "FROM stage_event e JOIN stage_run r ON r.run_id = e.run_id "
        f"WHERE {where} AND e.event = 'asr.discovery.completed' "
        "ORDER BY e.id DESC LIMIT 1",
        params,
    )
    if row is None:
        return None
    value = _load_json_dict(row["data_json"]).get("candidate_count")
    return int(value) if value is not None else None


async def _load_recent_events(
    main: Connection,
    *,
    uid: int,
    run_id: str | None,
    limit: int,
) -> list[RunEventSummary]:
    where, params = _event_where(uid=uid, run_id=run_id)
    rows = await main.fetch_all(
        "SELECT e.id, e.ts_ms, e.level, e.stage, e.event, e.endpoint, "
        "       e.pipeline, e.item_type, e.item_id, e.message, e.data_json "
        "FROM stage_event e JOIN stage_run r ON r.run_id = e.run_id "
        f"WHERE {where} "
        "ORDER BY e.id DESC LIMIT ?",
        (*params, max(0, int(limit))),
    )
    return [_event_from_row(row) for row in reversed(rows)]


async def _load_attention_events(
    main: Connection,
    *,
    uid: int,
    run_id: str | None,
    limit: int,
) -> list[RunEventSummary]:
    where, params = _event_where(uid=uid, run_id=run_id)
    attention = (
        "e.level IN ('WARNING', 'ERROR', 'CRITICAL') "
        "OR e.event LIKE '%.retry_scheduled' "
        "OR e.event LIKE '%.rate_limited' "
        "OR e.event LIKE '%.high_risk_%' "
        "OR e.event LIKE '%.failed'"
    )
    rows = await main.fetch_all(
        "SELECT e.id, e.ts_ms, e.level, e.stage, e.event, e.endpoint, "
        "       e.pipeline, e.item_type, e.item_id, e.message, e.data_json "
        "FROM stage_event e JOIN stage_run r ON r.run_id = e.run_id "
        f"WHERE {where} AND ({attention}) "
        "ORDER BY e.id DESC LIMIT ?",
        (*params, max(0, int(limit))),
    )
    return [_event_from_row(row) for row in reversed(rows)]


def _event_where(*, uid: int, run_id: str | None) -> tuple[str, tuple[Any, ...]]:
    if run_id is not None:
        return "e.run_id = ?", (run_id,)
    return "r.uid = ?", (uid,)


def _event_from_row(row: Any) -> RunEventSummary:
    return RunEventSummary(
        id=int(row["id"]),
        ts_ms=int(row["ts_ms"]),
        level=str(row["level"]),
        stage=str(row["stage"]),
        event=str(row["event"]),
        endpoint=row["endpoint"],
        pipeline=row["pipeline"],
        item_type=row["item_type"],
        item_id=row["item_id"],
        message=row["message"],
        data=_load_json_dict(row["data_json"]),
    )


def _load_json_dict(value: str | None) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_optional_json_dict(value: str | None) -> dict[str, Any] | None:
    loaded = _load_json_dict(value)
    return loaded or None


def _count_by_status(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = getattr(item, "status", None)
        if status is None:
            continue
        status_text = str(status)
        counts[status_text] = counts.get(status_text, 0) + 1
    return counts
