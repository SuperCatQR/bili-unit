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
from .dashboard import (
    DashboardSnapshot,
    ManifestSnapshot,
    RecommendedAction,
    UidDashboardSnapshot,
    load_dashboard_snapshot,
    load_uid_dashboard_snapshot,
)
from .summary import (
    AsrSummary,
    FetchEndpointSummary,
    FetchSummary,
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
    "DashboardSnapshot",
    "EventLevel",
    "EventSink",
    "FetchEndpointSummary",
    "FetchSummary",
    "JsonlSink",
    "LoggingSink",
    "ManifestSnapshot",
    "MemorySink",
    "NullSink",
    "RecommendedAction",
    "RunContext",
    "RunEventSummary",
    "RunEvent",
    "RunRecord",
    "RunReporter",
    "RunSummary",
    "RunStatus",
    "StageTaskSummary",
    "SqliteSink",
    "UidDashboardSnapshot",
    "build_run_summary",
    "load_dashboard_snapshot",
    "load_run_summary",
    "load_uid_dashboard_snapshot",
    "now_ms",
]
