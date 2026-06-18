"""Minimal TUI surface specification.

This module intentionally contains no UI framework code. It pins the first
interactive surface to stable workbench/read-model concepts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TuiPanelSpec:
    id: str
    title: str
    source: str


@dataclass(frozen=True)
class TuiActionSpec:
    id: str
    label: str
    stages: tuple[str, ...]
    command_method: str
    default_args: dict[str, object]


UID_LIST_PANEL = TuiPanelSpec(
    id="uid_list",
    title="UIDs",
    source="BiliWorkbench.dashboard().items",
)
STATUS_PANEL = TuiPanelSpec(
    id="status",
    title="Status",
    source="UidDashboardSnapshot.manifest + active_stages",
)
RUN_PANEL = TuiPanelSpec(
    id="run",
    title="Run",
    source="UidDashboardSnapshot.run_summary",
)
ATTENTION_PANEL = TuiPanelSpec(
    id="attention",
    title="Attention",
    source="RunSummary.recent_attention_events",
)
EVENT_PANEL = TuiPanelSpec(
    id="events",
    title="Events",
    source="RunSummary.recent_events",
)

TUI_MVP_PANELS: tuple[TuiPanelSpec, ...] = (
    UID_LIST_PANEL,
    STATUS_PANEL,
    RUN_PANEL,
    ATTENTION_PANEL,
    EVENT_PANEL,
)

TUI_MVP_ACTIONS: tuple[TuiActionSpec, ...] = (
    TuiActionSpec(
        id="sync",
        label="Sync",
        stages=("fetching", "parsing"),
        command_method="sync",
        default_args={
            "fetch_mode": "incremental",
            "parse_mode": "incremental",
        },
    ),
    TuiActionSpec(
        id="fetch",
        label="Fetch",
        stages=("fetching",),
        command_method="fetch",
        default_args={"mode": "incremental"},
    ),
    TuiActionSpec(
        id="parse",
        label="Parse",
        stages=("parsing",),
        command_method="parse",
        default_args={"mode": "incremental"},
    ),
    TuiActionSpec(
        id="asr",
        label="ASR",
        stages=("asr",),
        command_method="asr",
        default_args={"mode": "incremental"},
    ),
    TuiActionSpec(
        id="delete_uid",
        label="Delete UID",
        stages=(),
        command_method="delete_uid",
        default_args={},
    ),
)

__all__ = [
    "TUI_MVP_ACTIONS",
    "TUI_MVP_PANELS",
    "TuiActionSpec",
    "TuiPanelSpec",
]
