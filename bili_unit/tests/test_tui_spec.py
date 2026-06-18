from __future__ import annotations

from bili_unit.tui_spec import (
    TUI_DETAIL_TABS,
    TUI_KEYBINDINGS,
    TUI_LAYOUT,
    TUI_MVP_ACTIONS,
    TUI_MVP_PANELS,
)
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
    assert actions["sync"].key == "s"
    assert actions["sync"].safety == "preflight"
    assert actions["fetch"].stages == ("fetching",)
    assert actions["fetch"].default_args == {"mode": "incremental"}
    assert actions["fetch"].key == "f"
    assert actions["fetch"].safety == "preflight"
    assert actions["parse"].stages == ("parsing",)
    assert actions["parse"].default_args == {"mode": "incremental"}
    assert actions["parse"].key == "p"
    assert actions["parse"].safety == "preflight"
    assert actions["asr"].stages == ("asr",)
    assert actions["asr"].default_args == {"mode": "incremental"}
    assert actions["asr"].key == "a"
    assert actions["asr"].safety == "preflight"
    assert actions["delete_uid"].stages == ()
    assert actions["delete_uid"].default_args == {}
    assert actions["delete_uid"].key == "d"
    assert actions["delete_uid"].safety == "confirm"


def test_tui_layout_regions_are_stable() -> None:
    assert [region.id for region in TUI_LAYOUT] == [
        "uid_sidebar",
        "main_detail",
        "action_bar",
        "status_bar",
    ]
    assert all(region.min_width > 0 for region in TUI_LAYOUT)
    assert all(region.min_height > 0 for region in TUI_LAYOUT)


def test_tui_detail_tabs_are_stable() -> None:
    assert [tab.id for tab in TUI_DETAIL_TABS] == [
        "summary",
        "fetch",
        "parse",
        "asr",
        "events",
    ]
    assert all(tab.title for tab in TUI_DETAIL_TABS)
    assert all(tab.source for tab in TUI_DETAIL_TABS)


def test_tui_keybindings_cover_navigation_and_actions() -> None:
    bindings = {binding.action: binding for binding in TUI_KEYBINDINGS}

    for action in [
        "quit",
        "refresh",
        "select_next_uid",
        "select_previous_uid",
        "next_detail_tab",
        "previous_detail_tab",
        "sync",
        "fetch",
        "parse",
        "asr",
        "delete_uid",
    ]:
        assert action in bindings
        assert bindings[action].key
        assert bindings[action].description
