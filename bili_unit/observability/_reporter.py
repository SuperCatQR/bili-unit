from __future__ import annotations

from typing import Any

from ._events import EventLevel, RunContext, RunEvent, RunStatus
from ._sinks import EventSink, NullSink


class RunReporter:
    """Small facade used by command/runner code to emit run facts."""

    def __init__(self, context: RunContext, sink: EventSink | None = None) -> None:
        self.context = context
        self._sink = sink or NullSink()
        self._started = False
        self._completed = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._sink.start_run(self.context)

    async def emit(
        self,
        event: str,
        *,
        stage: str,
        level: EventLevel = "INFO",
        endpoint: str | None = None,
        pipeline: str | None = None,
        item_type: str | None = None,
        item_id: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        if not self._started:
            await self.start()
        run_event = RunEvent.from_context(
            self.context,
            stage=stage,
            event=event,
            level=level,
            endpoint=endpoint,
            pipeline=pipeline,
            item_type=item_type,
            item_id=item_id,
            message=message,
            data=data,
        )
        await self._sink.emit(run_event)
        return run_event

    async def complete(
        self,
        status: RunStatus,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if not self._started:
            await self.start()
        if self._completed:
            return
        self._completed = True
        await self._sink.complete_run(self.context, status, summary=summary)
