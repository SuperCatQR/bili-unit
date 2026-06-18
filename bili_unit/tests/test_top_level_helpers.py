"""Top-level helper surface self-check: ``__version__`` exists, every name in
``__all__`` is importable, and the load-bearing helpers are reachable.
"""
from __future__ import annotations

import importlib

import bili_unit


def test_version_string() -> None:
    assert isinstance(bili_unit.__version__, str)
    assert bili_unit.__version__


def test_all_names_importable() -> None:
    """``from bili_unit import <name>`` works for every name in __all__."""
    mod = importlib.import_module("bili_unit")
    for name in bili_unit.__all__:
        assert hasattr(mod, name), (
            f"{name!r} declared in __all__ but missing from module"
        )


def test_expected_top_level_names() -> None:
    """Pin the exact set of top-level names so accidental additions /
    removals show up in CI."""
    expected = {
        "ASRCommand",
        "ASRCommandResult",
        "ASRError",
        "BiliCommand",
        "BiliWorkbench",
        "BiliSettings",
        "CommandResult",
        "CredentialProvider",
        "ParsingCommandResult",
        "ProcessingCommandResult",
        "SyncCommandResult",
        "TaskResult",
        "TaskStartCheck",
        "TaskStatus",
        "TUI_MVP_ACTIONS",
        "TUI_MVP_PANELS",
        "UidContext",
        "AudioError",
        "FetchingError",
        "ParsingError",
        "ProcessingError",
        "__version__",
        "assemble",
        "assemble_workbench",
        "db_path",
        "get_settings",
        "list_uids",
        "raw_db_path",
        "reload_settings",
        "session",
        "workbench_session",
    }
    assert set(bili_unit.__all__) == expected


def test_key_types_at_top_level() -> None:
    """The helpers used by the CLI and advanced scripts are reachable from
    ``bili_unit`` directly."""
    from bili_unit import (
        BiliCommand,
        BiliWorkbench,
        BiliSettings,
        CommandResult,
        ParsingCommandResult,
        ProcessingCommandResult,
        SyncCommandResult,
        TaskStartCheck,
        TUI_MVP_ACTIONS,
        TUI_MVP_PANELS,
        UidContext,
        assemble,
        assemble_workbench,
        db_path,
        raw_db_path,
        session,
        workbench_session,
    )

    assert BiliCommand is not None
    assert BiliWorkbench is not None
    assert BiliSettings is not None
    assert CommandResult is not None
    assert ParsingCommandResult is not None
    assert ProcessingCommandResult is not None
    assert SyncCommandResult is not None
    assert TaskStartCheck is not None
    assert TUI_MVP_ACTIONS
    assert TUI_MVP_PANELS
    assert UidContext is not None
    assert callable(assemble)
    assert callable(assemble_workbench)
    assert callable(session)
    assert callable(workbench_session)
    assert callable(db_path)
    assert callable(raw_db_path)
