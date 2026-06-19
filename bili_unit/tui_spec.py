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
    key: str
    safety: str


@dataclass(frozen=True)
class TuiRegionSpec:
    id: str
    title: str
    role: str
    min_width: int
    min_height: int


@dataclass(frozen=True)
class TuiKeybindingSpec:
    key: str
    action: str
    description: str


@dataclass(frozen=True)
class TuiDetailTabSpec:
    id: str
    title: str
    source: str


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
    # Default mode across stage actions is "incremental": an interactive
    # workbench should always do the cheap, safe thing on a single keypress
    # and let users escalate to "full" via the CLI when they really want a
    # forced rerun.
    TuiActionSpec(
        id="add_uid",
        label="Add UID",
        stages=("fetching",),
        command_method="fetch",
        default_args={"mode": "incremental"},
        key="n",
        safety="prompt_preflight",
    ),
    TuiActionSpec(
        id="fetch",
        label="Fetch",
        stages=("fetching",),
        command_method="fetch",
        default_args={"mode": "incremental"},
        key="f",
        safety="preflight",
    ),
    TuiActionSpec(
        id="asr",
        label="ASR",
        stages=("asr",),
        command_method="asr",
        default_args={"mode": "incremental"},
        key="a",
        safety="preflight",
    ),
    TuiActionSpec(
        id="delete_uid",
        label="Delete UID",
        stages=(),
        command_method="delete_uid",
        default_args={},
        key="d",
        safety="confirm",
    ),
)

UID_SIDEBAR_REGION = TuiRegionSpec(
    id="uid_sidebar",
    title="UIDs",
    role="navigation",
    min_width=24,
    min_height=10,
)
MAIN_DETAIL_REGION = TuiRegionSpec(
    id="main_detail",
    title="Details",
    role="selected uid detail",
    min_width=56,
    min_height=16,
)
ACTION_BAR_REGION = TuiRegionSpec(
    id="action_bar",
    title="Actions",
    role="command palette",
    min_width=56,
    min_height=3,
)
STATUS_BAR_REGION = TuiRegionSpec(
    id="status_bar",
    title="Status",
    role="feedback",
    min_width=56,
    min_height=1,
)

TUI_LAYOUT: tuple[TuiRegionSpec, ...] = (
    UID_SIDEBAR_REGION,
    MAIN_DETAIL_REGION,
    ACTION_BAR_REGION,
    STATUS_BAR_REGION,
)

SUMMARY_TAB = TuiDetailTabSpec(
    id="summary",
    title="Summary",
    source="UidDashboardSnapshot.manifest + RunSummary stage rollups",
)
FETCH_TAB = TuiDetailTabSpec(
    id="fetch",
    title="Fetch",
    source="RunSummary.fetch.endpoints",
)
ASR_TAB = TuiDetailTabSpec(
    id="asr",
    title="ASR",
    source="RunSummary.asr coverage + recommended actions",
)
EVENTS_TAB = TuiDetailTabSpec(
    id="events",
    title="Events",
    source="RunSummary.recent_attention_events + recent_events",
)

TUI_DETAIL_TABS: tuple[TuiDetailTabSpec, ...] = (
    SUMMARY_TAB,
    FETCH_TAB,
    ASR_TAB,
    EVENTS_TAB,
)

TUI_KEYBINDINGS: tuple[TuiKeybindingSpec, ...] = (
    TuiKeybindingSpec("q", "quit", "Quit the TUI"),
    TuiKeybindingSpec("r", "refresh", "Reload dashboard snapshot"),
    TuiKeybindingSpec("j/down", "select_next_uid", "Select next uid"),
    TuiKeybindingSpec("k/up", "select_previous_uid", "Select previous uid"),
    TuiKeybindingSpec("tab", "next_detail_tab", "Move to next detail tab"),
    TuiKeybindingSpec("shift+tab", "previous_detail_tab", "Move to previous detail tab"),
    TuiKeybindingSpec("n", "add_uid", "Enter a new uid and run incremental fetch"),
    TuiKeybindingSpec("f", "fetch", "Run incremental fetch after preflight"),
    TuiKeybindingSpec("a", "asr", "Run incremental ASR after preflight"),
    TuiKeybindingSpec("d", "delete_uid", "Delete uid after confirmation"),
)

__all__ = [
    "TUI_MVP_ACTIONS",
    "TUI_MVP_PANELS",
    "TUI_DETAIL_TABS",
    "TUI_KEYBINDINGS",
    "TUI_LAYOUT",
    "TuiActionSpec",
    "TuiDetailTabSpec",
    "TuiKeybindingSpec",
    "TuiPanelSpec",
    "TuiRegionSpec",
]
