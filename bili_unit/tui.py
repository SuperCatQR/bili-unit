"""Minimal line-mode TUI over the workbench dashboard read model."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .observability.dashboard import DashboardSnapshot, UidDashboardSnapshot
from .tui_spec import TUI_DETAIL_TABS, TUI_MVP_ACTIONS, TuiActionSpec
from .workbench import BiliWorkbench, workbench_session


@dataclass
class TuiState:
    snapshot: DashboardSnapshot | None = None
    selected_index: int = 0
    selected_tab_index: int = 0
    message: str = "r refresh | tab next | s/f/p/a run | d delete | q quit"


@dataclass(frozen=True)
class TuiInputResult:
    should_quit: bool = False
    needs_refresh: bool = False
    action_id: str | None = None


@dataclass(frozen=True)
class TuiScreen:
    lines: list[str]
    width: int
    height: int


async def run_tui() -> None:
    """Run a portable interactive terminal dashboard.

    This intentionally avoids external UI dependencies. It gives Windows users
    a working first TUI surface now, while keeping the read model isolated so a
    richer Textual-style interface can replace the renderer later.
    """
    state = TuiState()
    async with workbench_session() as workbench:
        state.snapshot = await workbench.dashboard()
        while True:
            _print_screen(state)
            command = input("> ").strip().lower()
            result = handle_input(state, command)
            if result.should_quit:
                return
            if result.needs_refresh:
                await refresh_state(workbench, state)
                continue
            if result.action_id is not None:
                await dispatch_action(workbench, state, result.action_id, input)
                continue


def handle_input(state: TuiState, command: str) -> TuiInputResult:
    """Apply a line-mode command to state and report async work to perform."""
    if command in {"q", "quit", "exit"}:
        return TuiInputResult(should_quit=True)
    if command in {"", "r", "refresh"}:
        return TuiInputResult(needs_refresh=True)
    if command in {"tab", "next"}:
        state.selected_tab_index = _select_tab_index(state, state.selected_tab_index + 1)
        state.message = f"tab {current_tab_id(state)}"
        return TuiInputResult()
    if command in {"backtab", "prev", "previous"}:
        state.selected_tab_index = _select_tab_index(state, state.selected_tab_index - 1)
        state.message = f"tab {current_tab_id(state)}"
        return TuiInputResult()
    if command in {"j", "down"}:
        state.selected_index = _select_index(state, state.selected_index + 2)
        state.message = f"selected {state.selected_index + 1}"
        return TuiInputResult()
    if command in {"k", "up"}:
        state.selected_index = _select_index(state, state.selected_index)
        state.message = f"selected {state.selected_index + 1}"
        return TuiInputResult()
    if command.isdigit():
        state.selected_index = _select_index(state, int(command))
        state.message = f"selected {state.selected_index + 1}"
        return TuiInputResult()
    action = _action_by_key(command)
    if action is not None:
        return TuiInputResult(action_id=action.id)
    state.message = "unknown command"
    return TuiInputResult()


async def refresh_state(workbench: BiliWorkbench, state: TuiState) -> None:
    state.message = "refreshing..."
    state.snapshot = await workbench.dashboard()
    state.selected_index = _clamp_selection(state)
    state.selected_tab_index = _select_tab_index(state, state.selected_tab_index)
    state.message = "refreshed"


async def dispatch_action(
    workbench: BiliWorkbench,
    state: TuiState,
    action_id: str,
    ask: Callable[[str], str] = input,
) -> None:
    """Run one TUI action for the selected uid, then refresh the dashboard."""
    item = selected_item(state)
    if item is None:
        state.message = "no uid selected"
        return
    action = _action_by_id(action_id)
    if action is None:
        state.message = f"unknown action: {action_id}"
        return
    if action.safety == "confirm":
        expected = f"delete {item.uid}"
        answer = ask(f"Type '{expected}' to confirm: ").strip().lower()
        if answer != expected:
            state.message = "cancelled"
            return
    elif action.safety == "preflight":
        check = await workbench.can_start_task(item.uid, stages=action.stages)
        if not check.can_start:
            state.message = check.reason or "task blocked"
            return

    state.message = f"running {action.id}..."
    await _run_action(workbench, item.uid, action)
    state.snapshot = await workbench.dashboard()
    state.selected_index = _clamp_selection(state)
    state.message = f"{action.label} completed"


def render_lines(
    snapshot: DashboardSnapshot | None,
    *,
    selected_index: int = 0,
    selected_tab_index: int = 0,
    width: int = 100,
) -> list[str]:
    """Render dashboard state into plain text lines for TUI and tests."""
    return render_screen(
        snapshot,
        selected_index=selected_index,
        selected_tab_index=selected_tab_index,
        width=width,
    ).lines


def render_screen(
    snapshot: DashboardSnapshot | None,
    *,
    selected_index: int = 0,
    selected_tab_index: int = 0,
    width: int = 100,
    height: int | None = None,
) -> TuiScreen:
    """Render a stable sidebar/detail/action/status screen."""
    width = max(60, width)
    if snapshot is None:
        lines = ["bili-unit workbench", "", "Loading dashboard..."]
        return _screen(lines, width=width, height=height)
    items = snapshot.items
    header = _fit(f"bili-unit workbench  root={snapshot.root}", width)
    if not items:
        lines = [
            header,
            _rule(width),
            "No uid DBs found.",
            _rule(width),
            "Actions: r refresh | q quit",
            "Status: no uid selected",
        ]
        return _screen(lines, width=width, height=height)

    selected_index = max(0, min(selected_index, len(items) - 1))
    selected = items[selected_index]
    sidebar_width = _sidebar_width(width)
    detail_width = width - sidebar_width - 3
    sidebar = _sidebar_lines(items, selected_index, sidebar_width)
    detail = _detail_lines(selected, selected_tab_index, detail_width)
    body_height = max(len(sidebar), len(detail))
    body: list[str] = []
    for idx in range(body_height):
        left = sidebar[idx] if idx < len(sidebar) else ""
        right = detail[idx] if idx < len(detail) else ""
        body.append(f"{_pad(left, sidebar_width)} | {_fit(right, detail_width)}")

    actions = "Actions: " + "  ".join(
        f"{action.key}={action.label}"
        for action in TUI_MVP_ACTIONS
    )
    status = f"Status: uid={selected.uid} tab={TUI_DETAIL_TABS[_select_tab_index_for_count(selected_tab_index)].id}"
    lines = [
        header,
        _rule(width),
        *body,
        _rule(width),
        _fit(actions, width),
        _fit(status, width),
    ]
    return _screen(lines, width=width, height=height)


def _sidebar_lines(
    items: list[UidDashboardSnapshot],
    selected_index: int,
    width: int,
) -> list[str]:
    lines = ["UIDs"]
    for idx, item in enumerate(items):
        marker = ">" if idx == selected_index else " "
        lines.append(_fit(f"{idx + 1}. {marker} {_uid_line(item)}", width))
    return lines


def _detail_lines(
    item: UidDashboardSnapshot,
    selected_tab_index: int,
    width: int,
) -> list[str]:
    lines = [
        _fit(_tab_header(selected_tab_index), width),
        "",
        *_tab_lines(item, selected_tab_index),
        "",
        "Actions",
        *_action_lines(item),
    ]
    return [_fit(line, width) for line in lines]


def _print_screen(state: TuiState) -> None:
    print("\n" * 2)
    for line in render_lines(
        state.snapshot,
        selected_index=state.selected_index,
        selected_tab_index=state.selected_tab_index,
    ):
        print(line)
    print("")
    print(state.message)


def _uid_line(item: UidDashboardSnapshot) -> str:
    if not item.available:
        return f"{item.uid}  unavailable: {item.read_error}"
    manifest = item.manifest
    videos = manifest.video_count if manifest is not None else 0
    articles = manifest.article_count if manifest is not None else 0
    asr = manifest.transcribed_count if manifest is not None else 0
    active = ",".join(item.active_stages) if item.active_stages else "-"
    return f"{item.uid}  videos={videos} articles={articles} asr={asr} active={active}"


def _tab_header(selected_index: int) -> str:
    tab_parts = []
    for idx, tab in enumerate(TUI_DETAIL_TABS):
        title = f"[{tab.title}]" if idx == selected_index else tab.title
        tab_parts.append(title)
    return "Tabs: " + " | ".join(tab_parts)


def _tab_lines(item: UidDashboardSnapshot, selected_tab_index: int) -> list[str]:
    tab = TUI_DETAIL_TABS[_select_tab_index_for_count(selected_tab_index)]
    if tab.id == "summary":
        return _summary_lines(item)
    if tab.id == "fetch":
        return _fetch_lines(item)
    if tab.id == "parse":
        return _parse_lines(item)
    if tab.id == "asr":
        return _asr_lines(item)
    if tab.id == "events":
        return [*_attention_lines(item), "", *_event_lines(item)]
    return ["unknown tab"]


def _summary_lines(item: UidDashboardSnapshot) -> list[str]:
    if not item.available:
        return [f"read error: {item.read_error}"]
    manifest = item.manifest
    summary = item.run_summary
    lines: list[str] = []
    if manifest is not None:
        lines.append(
            "content: "
            f"videos={manifest.video_count} "
            f"articles={manifest.article_count} "
            f"opus={manifest.opus_count} "
            f"dynamics={manifest.dynamic_count}",
        )
        lines.append(
            "asr: "
            f"success={manifest.transcribed_count} "
            f"failed={manifest.transcription_failed_count} "
            f"tokens={manifest.total_audio_tokens}",
        )
    if summary is not None:
        latest = summary.run.run_id if summary.run is not None else "-"
        lines.append(f"latest run: {latest}")
        lines.append(
            "stage: "
            f"fetch={summary.fetch.status or '-'} "
            f"parse={summary.parse.status or '-'} "
            f"asr={summary.asr.status or '-'}",
        )
    return lines or ["no status"]


def _fetch_lines(item: UidDashboardSnapshot) -> list[str]:
    summary = item.run_summary
    if summary is None:
        return ["no fetch summary"]
    if not summary.fetch.endpoints:
        return [f"status={summary.fetch.status or '-'}", "no endpoint rows"]
    lines = [f"status={summary.fetch.status or '-'}"]
    for endpoint in summary.fetch.endpoints[:12]:
        progress = endpoint.item_progress or endpoint.progress or {}
        suffix = f" {progress}" if progress else ""
        lines.append(f"{endpoint.endpoint}: {endpoint.status}{suffix}")
    return lines


def _parse_lines(item: UidDashboardSnapshot) -> list[str]:
    summary = item.run_summary
    if summary is None:
        return ["no parse summary"]
    if not summary.parse.models:
        return [f"status={summary.parse.status or '-'}", "no model rows"]
    lines = [f"status={summary.parse.status or '-'}"]
    lines.extend(
        f"{model.model}: {model.status} count={model.count}"
        for model in summary.parse.models
    )
    if summary.parse.images:
        lines.append(f"images: {summary.parse.images}")
    return lines


def _asr_lines(item: UidDashboardSnapshot) -> list[str]:
    summary = item.run_summary
    if summary is None:
        return ["no ASR summary"]
    asr = summary.asr
    lines = [
        f"status={asr.status or '-'} candidates={asr.candidate_count or '-'}",
        (
            "coverage: "
            f"success={asr.success}/{asr.expected} "
            f"missing={asr.missing} failed={asr.failed} skipped={asr.skipped}"
        ),
    ]
    if asr.failed_bvids:
        lines.append("failed: " + " ".join(asr.failed_bvids[:8]))
    if asr.missing_bvids:
        lines.append("missing: " + " ".join(asr.missing_bvids[:8]))
    return lines


def _action_lines(item: UidDashboardSnapshot) -> list[str]:
    lines = [
        f"{action.key} {action.label} ({action.safety})"
        for action in TUI_MVP_ACTIONS
        if action.id != "delete_uid"
    ]
    lines.append("d Delete UID (confirm)")
    if item.recommended_actions:
        lines.append("recommended:")
        lines.extend(
            f"{action.label}: {action.command}"
            for action in item.recommended_actions
        )
    return lines


def _attention_lines(item: UidDashboardSnapshot) -> list[str]:
    summary = item.run_summary
    if summary is None or not summary.recent_attention_events:
        return ["no attention events"]
    return [
        f"{event.level} {event.event} {event.item_id or event.endpoint or ''}".strip()
        for event in summary.recent_attention_events[-5:]
    ]


def _event_lines(item: UidDashboardSnapshot) -> list[str]:
    summary = item.run_summary
    if summary is None or not summary.recent_events:
        return ["no recent events"]
    return [
        f"{event.level} {event.event} {event.item_id or event.endpoint or ''}".strip()
        for event in summary.recent_events[-5:]
    ]


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)]


def _pad(text: str, width: int) -> str:
    text = _fit(text, width)
    return text + (" " * max(0, width - len(text)))


def _rule(width: int) -> str:
    return "-" * max(1, width)


def _sidebar_width(width: int) -> int:
    return max(24, min(38, width // 3))


def _screen(
    lines: list[str],
    *,
    width: int,
    height: int | None,
) -> TuiScreen:
    fitted = [_fit(line, width) for line in lines]
    if height is not None:
        target = max(1, height)
        fitted = fitted[:target]
        fitted.extend("" for _ in range(target - len(fitted)))
    return TuiScreen(lines=fitted, width=width, height=len(fitted))


def _select_index(state: TuiState, one_based_index: int) -> int:
    return max(0, min(one_based_index - 1, _item_count(state) - 1))


def _select_tab_index(state: TuiState, index: int) -> int:
    return _select_tab_index_for_count(index)


def _select_tab_index_for_count(index: int) -> int:
    return index % len(TUI_DETAIL_TABS)


def current_tab_id(state: TuiState) -> str:
    return TUI_DETAIL_TABS[_select_tab_index(state, state.selected_tab_index)].id


def selected_item(state: TuiState) -> UidDashboardSnapshot | None:
    if state.snapshot is None or not state.snapshot.items:
        return None
    return state.snapshot.items[_clamp_selection(state)]


def _item_count(state: TuiState) -> int:
    if state.snapshot is None:
        return 1
    return max(1, len(state.snapshot.items))


def _clamp_selection(state: TuiState) -> int:
    return max(0, min(state.selected_index, _item_count(state) - 1))


def _action_by_key(key: str) -> TuiActionSpec | None:
    for action in TUI_MVP_ACTIONS:
        if action.key == key:
            return action
    return None


def _action_by_id(action_id: str) -> TuiActionSpec | None:
    for action in TUI_MVP_ACTIONS:
        if action.id == action_id:
            return action
    return None


async def _run_action(
    workbench: BiliWorkbench,
    uid: int,
    action: TuiActionSpec,
) -> None:
    method = getattr(workbench, action.command_method)
    await method(uid, **action.default_args)


__all__ = [
    "TuiInputResult",
    "TuiScreen",
    "TuiState",
    "current_tab_id",
    "dispatch_action",
    "handle_input",
    "refresh_state",
    "render_lines",
    "render_screen",
    "run_tui",
    "selected_item",
]
