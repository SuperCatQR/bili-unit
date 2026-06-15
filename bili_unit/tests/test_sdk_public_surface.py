"""SDK top-level surface self-check: ``__version__`` exists, every name in
``__all__`` is importable, and the load-bearing public types are at the top
level.

Phase 6 rewrite — the read-side facade (``BiliQuery`` / ``ParsingTaskDTO`` /
``VideoFullDTO`` / ``ProcessingPipelineStatus`` re-exports) is gone. The
expected exports list now matches the post-refactor ``bili_unit.__all__``.
"""
from __future__ import annotations

import importlib

import bili_unit


def test_version_string() -> None:
    assert isinstance(bili_unit.__version__, str)
    assert bili_unit.__version__  # non-empty


def test_all_names_importable() -> None:
    """``from bili_unit import <name>`` works for every name in __all__."""
    mod = importlib.import_module("bili_unit")
    for name in bili_unit.__all__:
        assert hasattr(mod, name), (
            f"{name!r} declared in __all__ but missing from module"
        )


def test_expected_public_surface() -> None:
    """Pin the exact set of post-refactor public names so accidental
    additions / removals show up in CI."""
    expected = {
        # write-side types
        "BiliCommand",
        "BiliSettings",
        "CommandResult",
        "CredentialProvider",
        "ParsingCommandResult",
        "ProcessingCommandResult",
        "TaskResult",
        "TaskStatus",
        "UidContext",
        # exceptions
        "AudioError",
        "FetchingError",
        "ParsingError",
        "ProcessingError",
        # entry points / helpers
        "__version__",
        "assemble",
        "db_path",
        "get_settings",
        "list_uids",
        "raw_db_path",
        "reload_settings",
        "session",
    }
    assert set(bili_unit.__all__) == expected


def test_key_types_at_top_level() -> None:
    """The handful of types every consumer needs are reachable from
    ``bili_unit`` directly (defends against future re-shuffling)."""
    from bili_unit import (
        BiliCommand,
        BiliSettings,
        CommandResult,
        ParsingCommandResult,
        ProcessingCommandResult,
        UidContext,
        assemble,
        db_path,
        raw_db_path,
        session,
    )
    assert BiliCommand is not None
    assert BiliSettings is not None
    assert CommandResult is not None
    assert ParsingCommandResult is not None
    assert ProcessingCommandResult is not None
    assert UidContext is not None
    # entry points are callables
    assert callable(assemble)
    assert callable(session)
    assert callable(db_path)
    assert callable(raw_db_path)
