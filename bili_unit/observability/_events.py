from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

EventLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RunStatus = Literal[
    "PENDING", "RUNNING", "SUCCESS", "PARTIAL", "FAILED", "CANCELLED", "DRY_RUN",
]


def now_ms() -> int:
    """Return current wall-clock time as ms since epoch."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class RunContext:
    """Identity and arguments for one command run."""

    run_id: str
    uid: int
    command: str
    args: dict[str, Any] = field(default_factory=dict)
    started_at_ms: int = field(default_factory=now_ms)

    @classmethod
    def create(
        cls,
        *,
        uid: int,
        command: str,
        args: dict[str, Any] | None = None,
        run_id: str | None = None,
        started_at_ms: int | None = None,
    ) -> RunContext:
        return cls(
            run_id=run_id or uuid.uuid4().hex,
            uid=uid,
            command=command,
            args=dict(args or {}),
            started_at_ms=now_ms() if started_at_ms is None else started_at_ms,
        )


@dataclass(frozen=True)
class RunEvent:
    """A semantic execution event emitted by a runner or command."""

    run_id: str
    uid: int
    stage: str
    event: str
    level: EventLevel = "INFO"
    ts_ms: int = field(default_factory=now_ms)
    endpoint: str | None = None
    pipeline: str | None = None
    item_type: str | None = None
    item_id: str | None = None
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(
        cls,
        context: RunContext,
        *,
        stage: str,
        event: str,
        level: EventLevel = "INFO",
        endpoint: str | None = None,
        pipeline: str | None = None,
        item_type: str | None = None,
        item_id: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        ts_ms: int | None = None,
    ) -> RunEvent:
        return cls(
            run_id=context.run_id,
            uid=context.uid,
            stage=stage,
            event=event,
            level=level,
            ts_ms=now_ms() if ts_ms is None else ts_ms,
            endpoint=endpoint,
            pipeline=pipeline,
            item_type=item_type,
            item_id=item_id,
            message=message,
            data=dict(data or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "uid": self.uid,
            "stage": self.stage,
            "event": self.event,
            "level": self.level,
            "ts_ms": self.ts_ms,
            "endpoint": self.endpoint,
            "pipeline": self.pipeline,
            "item_type": self.item_type,
            "item_id": self.item_id,
            "message": self.message,
            "data": self.data,
        }
