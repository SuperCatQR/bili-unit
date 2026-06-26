from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from ._events import RunContext, RunEvent, RunStatus, now_ms


def _dump_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


class EventSink(Protocol):
    async def start_run(self, context: RunContext) -> None: ...

    async def emit(self, event: RunEvent) -> None: ...

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None: ...


class NullSink:
    async def start_run(self, context: RunContext) -> None:
        return None

    async def emit(self, event: RunEvent) -> None:
        return None

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        return None


class CompositeSink:
    """Fan out events to multiple sinks in order."""

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = list(sinks)

    async def start_run(self, context: RunContext) -> None:
        for sink in self._sinks:
            await sink.start_run(context)

    async def emit(self, event: RunEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        for sink in self._sinks:
            await sink.complete_run(context, status, summary=summary)


class MemorySink:
    """Test/helper sink that records calls in memory."""

    def __init__(self) -> None:
        self.started: list[RunContext] = []
        self.events: list[RunEvent] = []
        self.completed: list[tuple[RunContext, RunStatus, dict[str, Any] | None]] = []

    async def start_run(self, context: RunContext) -> None:
        self.started.append(context)

    async def emit(self, event: RunEvent) -> None:
        self.events.append(event)

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self.completed.append((context, status, summary))


class LoggingSink:
    """Bridge run events into Python logging."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("bili.observability")

    async def start_run(self, context: RunContext) -> None:
        self._logger.info(
            "run.started",
            extra={
                "run_id": context.run_id,
                "uid": context.uid,
                "command": context.command,
                "run_args": context.args,
            },
        )

    async def emit(self, event: RunEvent) -> None:
        level = getattr(logging, event.level, logging.INFO)
        extra = event.to_dict()
        extra["event_message"] = extra.pop("message")
        msg = event.message or event.event
        self._logger.log(level, msg, extra=extra)

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self._logger.info(
            "run.completed",
            extra={
                "run_id": context.run_id,
                "uid": context.uid,
                "command": context.command,
                "status": status,
                "summary": summary or {},
            },
        )


class JsonlSink:
    """Append run and event records to a JSON Lines file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def start_run(self, context: RunContext) -> None:
        await self._append(
            {
                "type": "run.started",
                "run_id": context.run_id,
                "uid": context.uid,
                "command": context.command,
                "args": context.args,
                "started_at_ms": context.started_at_ms,
            }
        )

    async def emit(self, event: RunEvent) -> None:
        payload = {"type": "event", **event.to_dict()}
        await self._append(payload)

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        await self._append(
            {
                "type": "run.completed",
                "run_id": context.run_id,
                "uid": context.uid,
                "command": context.command,
                "status": status,
                "summary": summary or {},
                "ended_at_ms": now_ms(),
            }
        )

    async def _append(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, default=str)
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")


class SqliteSink:
    """Persist run history to main DB producer-state tables."""

    def __init__(self, main: Any) -> None:
        self._main = main

    async def start_run(self, context: RunContext) -> None:
        await self._main.execute(
            "INSERT OR REPLACE INTO stage_run("
            "    run_id, uid, command, status, started_at_ms, ended_at_ms, "
            "    args_json, summary_json"
            ") VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            (
                context.run_id,
                context.uid,
                context.command,
                "RUNNING",
                context.started_at_ms,
                _dump_json(context.args),
            ),
        )

    async def emit(self, event: RunEvent) -> None:
        await self._main.execute(
            "INSERT INTO stage_event("
            "    run_id, ts_ms, level, stage, event, endpoint, pipeline, "
            "    item_type, item_id, message, data_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.run_id,
                event.ts_ms,
                event.level,
                event.stage,
                event.event,
                event.endpoint,
                event.pipeline,
                event.item_type,
                event.item_id,
                event.message,
                _dump_json(event.data),
            ),
        )

    async def complete_run(
        self,
        context: RunContext,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        await self._main.execute(
            "UPDATE stage_run SET status = ?, ended_at_ms = ?, summary_json = ? WHERE run_id = ?",
            (status, now_ms(), _dump_json(summary), context.run_id),
        )
