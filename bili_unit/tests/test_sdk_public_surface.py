"""SDK 顶层 surface 自检：__version__ 存在、__all__ 中每个名字真的能 import。"""
from __future__ import annotations

import importlib

import bili_unit


def test_version_string() -> None:
    assert isinstance(bili_unit.__version__, str)
    assert bili_unit.__version__  # 非空


def test_all_names_importable() -> None:
    """``from bili_unit import <name>`` 对 __all__ 中每个名字都成立。"""
    mod = importlib.import_module("bili_unit")
    for name in bili_unit.__all__:
        assert hasattr(mod, name), f"{name} declared in __all__ but missing from module"


def test_key_dtos_at_top_level() -> None:
    """关键 DTO / 类型必须能从顶层拿到（防回归）。"""
    from bili_unit import (
        BiliCommand,
        BiliQuery,
        BiliSettings,
        ParsingTaskDTO,
        ProcessingPipelineStatus,
        VideoFullDTO,
    )
    assert BiliCommand is not None
    assert BiliQuery is not None
    assert BiliSettings is not None
    assert ParsingTaskDTO is not None
    assert ProcessingPipelineStatus is not None
    assert VideoFullDTO is not None
