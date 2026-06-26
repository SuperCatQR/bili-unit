from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from bili_unit.observability.dashboard import (
    DashboardSnapshot,
    ManifestSnapshot,
    RecommendedAction,
    UidDashboardSnapshot,
)
from bili_unit.observability.summary import (
    AsrSummary,
    FetchSummary,
    RunSummary,
)
from bili_unit.tui import (
    TuiState,
    current_tab_id,
    dispatch_action,
    handle_input,
    render_lines,
    render_screen,
)
from bili_unit.workbench import TaskStartCheck


def test_render_lines_empty_dashboard(tmp_path: Path) -> None:
    lines = render_lines(
        DashboardSnapshot(root=tmp_path, uids=[], items=[]),
        width=300,
    )

    rendered = "\n".join(lines)
    assert "bili-unit workbench" in rendered
    assert f"root={tmp_path}" in rendered
    assert "No uid DBs found." in rendered
    assert "Actions: n add uid | r refresh | q quit" in rendered
    assert "Status: no uid selected" in rendered


def test_render_lines_uid_status_and_recommendations(tmp_path: Path) -> None:
    item = UidDashboardSnapshot(
        uid=123,
        db=tmp_path / "123.raw.db",
        workdir=tmp_path / "123",
        manifest=ManifestSnapshot(
            uid=123,
            endpoint_count=4,
            raw_payload_count=12,
            video_count=2,
            transcribed_count=1,
            transcription_failed_count=1,
            total_audio_tokens=9,
        ),
        run_summary=RunSummary(
            uid=123,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(status="SUCCESS"),
            asr=AsrSummary(status="PARTIAL"),
            recent_events=[],
            recent_attention_events=[],
        ),
        recommended_actions=[
            RecommendedAction(
                kind="asr_retry_failed",
                label="Retry failed ASR items",
                command="uv run bili-unit asr 123 --retry-failed-only",
            ),
        ],
    )

    lines = render_lines(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
    )

    rendered = "\n".join(lines)
    assert "1. > 123" in rendered
    assert "raw: endpoints=4 rows=12 videos=2" in rendered
    assert "asr: success=1 failed=1 tokens=9" in rendered
    assert "stage: fetch=SUCCESS asr=PARTIAL" in rendered
    assert "f Fetch (preflight)" in rendered
    assert "a ASR (preflight)" in rendered
    assert "d Delete UID (confirm)" in rendered
    assert "recommended:" in rendered
    assert "Retry failed ASR items: uv run bili-unit asr 123" in rendered


def test_render_screen_uses_sidebar_detail_action_and_status(tmp_path: Path) -> None:
    item = _uid_item(tmp_path, 123)
    screen = render_screen(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
        selected_tab_index=2,
        width=90,
    )
    rendered = "\n".join(screen.lines)

    assert screen.width == 90
    assert all(len(line) <= 90 for line in screen.lines)
    assert "UIDs" in rendered
    assert "|" in rendered
    assert "Tabs: Summary | Fetch | [ASR] | Events" in rendered
    assert "Actions: n=Add UID  f=Fetch  a=ASR  d=Delete UID" in rendered
    assert "Status: uid=123 tab=asr" in rendered


def test_render_screen_respects_height(tmp_path: Path) -> None:
    item = _uid_item(tmp_path, 123)
    screen = render_screen(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
        width=80,
        height=6,
    )

    assert screen.height == 6
    assert len(screen.lines) == 6
    assert all(len(line) <= 80 for line in screen.lines)


def test_handle_input_navigation_and_actions(tmp_path: Path) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[1, 2],
            items=[
                _uid_item(tmp_path, 1),
                _uid_item(tmp_path, 2),
            ],
        ),
    )

    result = handle_input(state, "j")
    assert result == result.__class__()
    assert state.selected_index == 1

    handle_input(state, "k")
    assert state.selected_index == 0

    handle_input(state, "tab")
    assert current_tab_id(state) == "fetch"

    handle_input(state, "prev")
    assert current_tab_id(state) == "summary"

    assert handle_input(state, "r").needs_refresh is True
    assert handle_input(state, "n").action_id == "add_uid"
    assert handle_input(state, "f").action_id == "fetch"
    assert handle_input(state, "q").should_quit is True


def test_render_lines_respects_selected_tab(tmp_path: Path) -> None:
    item = _uid_item(tmp_path, 123)
    lines = render_lines(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
        selected_tab_index=2,
    )

    rendered = "\n".join(lines)
    assert "Tabs: Summary | Fetch | [ASR] | Events" in rendered
    assert "coverage: success=1/2 missing=1 failed=0 skipped=0" in rendered


async def test_dispatch_action_blocks_when_preflight_fails(tmp_path: Path) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=False)

    await dispatch_action(workbench, state, "fetch")

    assert state.message == "stage already running: fetching"
    assert workbench.calls == [("can_start_task", 123, ("fetching",))]


async def test_dispatch_action_runs_preflight_action_and_refreshes(
    tmp_path: Path,
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True)

    await dispatch_action(workbench, state, "fetch")

    assert state.message == "Fetch completed"
    assert workbench.calls == [
        ("can_start_task", 123, ("fetching",)),
        ("fetch", 123, {"mode": "incremental"}),
        ("dashboard",),
    ]


async def test_dispatch_action_add_uid_without_existing_selection(
    tmp_path: Path,
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(root=tmp_path, uids=[], items=[]),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True, dashboard_uid=456)

    await dispatch_action(workbench, state, "add_uid", ask=lambda _prompt: "456")

    assert state.message == "Add UID 456 completed"
    assert state.selected_index == 0
    assert state.snapshot is not None
    assert [item.uid for item in state.snapshot.items] == [456]
    assert workbench.calls == [
        ("can_start_task", 456, ("fetching",)),
        ("fetch", 456, {"mode": "incremental"}),
        ("dashboard",),
    ]


async def test_dispatch_action_add_uid_cancels_invalid_input(
    tmp_path: Path,
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(root=tmp_path, uids=[], items=[]),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True)

    await dispatch_action(workbench, state, "add_uid", ask=lambda _prompt: "nope")

    assert state.message == "cancelled"
    assert workbench.calls == []


async def test_dispatch_action_requires_delete_confirmation(tmp_path: Path) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True)

    await dispatch_action(workbench, state, "delete_uid", ask=lambda _prompt: "no")
    assert state.message == "cancelled"
    assert workbench.calls == []

    await dispatch_action(
        workbench,
        state,
        "delete_uid",
        ask=lambda _prompt: "delete 123",
    )

    assert state.message == "Delete UID completed"
    assert workbench.calls == [
        ("delete_uid", 123, {}),
        ("dashboard",),
    ]


def _uid_item(tmp_path: Path, uid: int) -> UidDashboardSnapshot:
    return UidDashboardSnapshot(
        uid=uid,
        db=tmp_path / f"{uid}.raw.db",
        workdir=tmp_path / str(uid),
        manifest=ManifestSnapshot(
            uid=uid,
            endpoint_count=2,
            raw_payload_count=4,
            video_count=2,
            transcribed_count=1,
            transcription_failed_count=0,
        ),
        run_summary=RunSummary(
            uid=uid,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(status="SUCCESS"),
            asr=AsrSummary(
                status="PARTIAL",
                expected=2,
                success=1,
                missing=1,
                missing_bvids=["BVm"],
            ),
            recent_events=[],
            recent_attention_events=[],
        ),
    )


class _FakeWorkbench:
    def __init__(
        self,
        root: Path,
        *,
        can_start: bool,
        dashboard_uid: int | None = None,
    ) -> None:
        self.root = root
        self.can_start = can_start
        self.dashboard_uid = dashboard_uid
        self.calls: list[tuple] = []

    async def can_start_task(self, uid: int, *, stages: tuple[str, ...]):
        self.calls.append(("can_start_task", uid, stages))
        return TaskStartCheck(
            uid=uid,
            can_start=self.can_start,
            active_stages=("fetching",) if not self.can_start else (),
            requested_stages=stages,
            reason=None if self.can_start else "stage already running: fetching",
        )

    async def fetch(self, uid: int, **kwargs):
        self.calls.append(("fetch", uid, kwargs))

    async def asr(self, uid: int, **kwargs):
        self.calls.append(("asr", uid, kwargs))

    async def delete_uid(self, uid: int, **kwargs):
        self.calls.append(("delete_uid", uid, kwargs))

    async def dashboard(self):
        self.calls.append(("dashboard",))
        if self.dashboard_uid is None:
            return DashboardSnapshot(root=self.root, uids=[], items=[])
        return DashboardSnapshot(
            root=self.root,
            uids=[self.dashboard_uid],
            items=[_uid_item(self.root, self.dashboard_uid)],
        )


class _ErrorWorkbench(_FakeWorkbench):
    """Variant where fetch raises a RuntimeError."""

    async def fetch(self, uid: int, **kwargs):
        raise RuntimeError("network timeout")


async def test_dispatch_action_catches_command_exception_and_keeps_tui_alive(
    tmp_path: Path,
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _ErrorWorkbench(tmp_path, can_start=True)

    # Must NOT raise
    await dispatch_action(workbench, state, "fetch")

    assert "failed" in state.message
    assert "RuntimeError" in state.message
    assert state.snapshot is not None
    assert ("dashboard",) in workbench.calls


async def test_dispatch_action_logs_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _ErrorWorkbench(tmp_path, can_start=True)

    with caplog.at_level(logging.ERROR, logger="bili.tui"):
        await dispatch_action(workbench, state, "fetch")

    assert any("failed" in r.message.lower() or "network timeout" in r.message for r in caplog.records)


def test_print_screen_uses_terminal_size(tmp_path: Path) -> None:
    import os

    from bili_unit import tui as tui_module

    item = _uid_item(tmp_path, 123)
    state = TuiState(
        snapshot=DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
    )
    fake_ts = os.terminal_size((80, 24))

    collected: list[str] = []

    def fake_print(*args, **kwargs):
        if args:
            collected.append(str(args[0]))

    with (
        patch("bili_unit.tui.shutil.get_terminal_size", return_value=fake_ts),
        patch("builtins.print", side_effect=fake_print),
    ):
        tui_module._print_screen(state)

    content_lines = [ln for ln in collected if ln]
    assert content_lines, "expected some rendered output"
    assert all(len(ln) <= 80 for ln in content_lines), f"line too wide: {max(content_lines, key=len)!r}"


async def test_dispatch_action_runs_asr(tmp_path: Path) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True)

    await dispatch_action(workbench, state, "asr")

    assert ("asr", 123, {"mode": "incremental"}) in workbench.calls
    assert "completed" in state.message
    assert ("dashboard",) in workbench.calls


async def test_dispatch_action_prints_running_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = TuiState(
        snapshot=DashboardSnapshot(
            root=tmp_path,
            uids=[123],
            items=[_uid_item(tmp_path, 123)],
        ),
    )
    workbench = _FakeWorkbench(tmp_path, can_start=True)

    await dispatch_action(workbench, state, "fetch")

    out = capsys.readouterr().out
    assert "running" in out
    assert "see stderr" in out
