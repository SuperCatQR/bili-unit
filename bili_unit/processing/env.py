# env — processing stage settings.
#
# Settings 全部住在 :mod:`bili_unit._env`；本文件保留做向后兼容的 stage-level
# 入口（docs/structure/bili.md §4 把 env 列为 stage 内模块）。

from .._env import BiliSettings as _BiliSettings
from .._env import get_settings as _get_settings
from .._env import reload_settings as _reload_settings

ProcessingEnv = _BiliSettings


def get_processing_settings() -> _BiliSettings:
    """Backward-compat entry: returns the same singleton as ``bili_unit._env.get_settings``."""
    return _get_settings()


def reload_processing_settings() -> None:
    """Backward-compat entry: same as ``bili_unit._env.reload_settings``."""
    _reload_settings()


__all__ = ["ProcessingEnv", "get_processing_settings", "reload_processing_settings"]
