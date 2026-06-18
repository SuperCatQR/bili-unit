from __future__ import annotations

from bili_unit.tui_spec import TUI_MVP_ACTIONS, TUI_MVP_PANELS
from bili_unit.workbench import BiliWorkbench


def test_tui_mvp_panels_are_ordered_for_first_screen() -> None:
    assert [panel.id for panel in TUI_MVP_PANELS] == [
        "uid_list",
        "status",
        "run",
        "attention",
        "events",
    ]
    assert all(panel.title for panel in TUI_MVP_PANELS)
    assert all(panel.source for panel in TUI_MVP_PANELS)


def test_tui_mvp_actions_map_to_workbench_methods() -> None:
    assert [action.id for action in TUI_MVP_ACTIONS] == [
        "sync",
        "fetch",
        "parse",
        "asr",
        "delete_uid",
    ]
    for action in TUI_MVP_ACTIONS:
        assert hasattr(BiliWorkbench, action.command_method)


def test_tui_mvp_actions_pin_preflight_stages_and_defaults() -> None:
    actions = {action.id: action for action in TUI_MVP_ACTIONS}

    assert actions["sync"].stages == ("fetching", "parsing")
    assert actions["sync"].default_args == {
        "fetch_mode": "incremental",
        "parse_mode": "incremental",
    }
    assert actions["fetch"].stages == ("fetching",)
    assert actions["fetch"].default_args == {"mode": "incremental"}
    assert actions["parse"].stages == ("parsing",)
    assert actions["parse"].default_args == {"mode": "incremental"}
    assert actions["asr"].stages == ("asr",)
    assert actions["asr"].default_args == {"mode": "incremental"}
    assert actions["delete_uid"].stages == ()
    assert actions["delete_uid"].default_args == {}
