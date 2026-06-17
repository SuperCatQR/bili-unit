"""Run observability primitives.

This package models execution facts separately from terminal rendering. Runners
can emit structured events through :class:`RunReporter`; sinks decide whether
those events become logging records, JSONL rows, SQLite history, or later TUI
updates.
"""

from ._events import EventLevel, RunContext, RunEvent, RunStatus, now_ms
from ._reporter import RunReporter
from ._sinks import (
    CompositeSink,
    EventSink,
    JsonlSink,
    LoggingSink,
    MemorySink,
    NullSink,
    SqliteSink,
)
from .summary import (
    AsrSummary,
    FetchEndpointSummary,
    FetchSummary,
    ParseModelSummary,
    ParseSummary,
    RunEventSummary,
    RunRecord,
    RunSummary,
    StageTaskSummary,
    build_run_summary,
    load_run_summary,
)

__all__ = [
    "AsrSummary",
    "CompositeSink",
    "EventLevel",
    "EventSink",
    "FetchEndpointSummary",
    "FetchSummary",
    "JsonlSink",
    "LoggingSink",
    "MemorySink",
    "NullSink",
    "ParseModelSummary",
    "ParseSummary",
    "RunContext",
    "RunEventSummary",
    "RunEvent",
    "RunRecord",
    "RunReporter",
    "RunSummary",
    "RunStatus",
    "StageTaskSummary",
    "SqliteSink",
    "build_run_summary",
    "load_run_summary",
    "now_ms",
]
