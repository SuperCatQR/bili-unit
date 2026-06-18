from __future__ import annotations

from pathlib import Path

from bili_unit.observability.dashboard import (
    DashboardSnapshot,
    ManifestSnapshot,
    RecommendedAction,
    UidDashboardSnapshot,
)
from bili_unit.observability.summary import (
    AsrSummary,
    FetchSummary,
    ParseSummary,
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
    assert "Actions: r refresh | q quit" in rendered
    assert "Status: no uid selected" in rendered


def test_render_lines_uid_status_and_recommendations(tmp_path: Path) -> None:
    item = UidDashboardSnapshot(
        uid=123,
        main_db=tmp_path / "123.db",
        raw_db=tmp_path / "123.raw.db",
        workdir=tmp_path / "123",
        manifest=ManifestSnapshot(
            uid=123,
            video_count=2,
            article_count=1,
            transcribed_count=1,
            transcription_failed_count=1,
            total_audio_tokens=9,
        ),
        run_summary=RunSummary(
            uid=123,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(status="SUCCESS"),
            parse=ParseSummary(status="SUCCESS"),
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
    assert "content: videos=2 articles=1 opus=0 dynamics=0" in rendered
    assert "asr: success=1 failed=1 tokens=9" in rendered
    assert "stage: fetch=SUCCESS parse=SUCCESS asr=PARTIAL" in rendered
    assert "s Sync (preflight)" in rendered
    assert "a ASR (preflight)" in rendered
    assert "d Delete UID (confirm)" in rendered
    assert "recommended:" in rendered
    assert "Retry failed ASR items: uv run bili-unit asr 123" in rendered


def test_render_screen_uses_sidebar_detail_action_and_status(tmp_path: Path) -> None:
    item = _uid_item(tmp_path, 123)
    screen = render_screen(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
        selected_tab_index=3,
        width=90,
    )
    rendered = "\n".join(screen.lines)

    assert screen.width == 90
    assert all(len(line) <= 90 for line in screen.lines)
    assert "UIDs" in rendered
    assert "|" in rendered
    assert "Tabs: Summary | Fetch | Parse | [ASR] | Events" in rendered
    assert "Actions: s=Sync  f=Fetch  p=Parse  a=ASR  d=Delete UID" in rendered
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
    assert handle_input(state, "s").action_id == "sync"
    assert handle_input(state, "q").should_quit is True


def test_render_lines_respects_selected_tab(tmp_path: Path) -> None:
    item = _uid_item(tmp_path, 123)
    lines = render_lines(
        DashboardSnapshot(root=tmp_path, uids=[123], items=[item]),
        selected_tab_index=3,
    )

    rendered = "\n".join(lines)
    assert "Tabs: Summary | Fetch | Parse | [ASR] | Events" in rendered
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

    await dispatch_action(workbench, state, "sync")

    assert state.message == "stage already running: fetching"
    assert workbench.calls == [("can_start_task", 123, ("fetching", "parsing"))]


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

    await dispatch_action(workbench, state, "sync")

    assert state.message == "Sync completed"
    assert workbench.calls == [
        ("can_start_task", 123, ("fetching", "parsing")),
        ("sync", 123, {"fetch_mode": "incremental", "parse_mode": "incremental"}),
        ("dashboard",),
    ]


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
        main_db=tmp_path / f"{uid}.db",
        raw_db=tmp_path / f"{uid}.raw.db",
        workdir=tmp_path / str(uid),
        manifest=ManifestSnapshot(
            uid=uid,
            video_count=2,
            article_count=1,
            transcribed_count=1,
            transcription_failed_count=0,
        ),
        run_summary=RunSummary(
            uid=uid,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(status="SUCCESS"),
            parse=ParseSummary(status="SUCCESS"),
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
    def __init__(self, root: Path, *, can_start: bool) -> None:
        self.root = root
        self.can_start = can_start
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

    async def sync(self, uid: int, **kwargs):
        self.calls.append(("sync", uid, kwargs))

    async def delete_uid(self, uid: int, **kwargs):
        self.calls.append(("delete_uid", uid, kwargs))

    async def dashboard(self):
        self.calls.append(("dashboard",))
        return DashboardSnapshot(root=self.root, uids=[], items=[])
