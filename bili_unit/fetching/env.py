# env — fetching stage settings.
#
# Settings 全部住在 :mod:`bili_unit._env`；本文件保留做向后兼容的 stage-level
# 入口（docs/structure/bili.md §4 把 env 列为 stage 内模块）。

from .._env import BiliSettings as _BiliSettings
from .._env import get_settings, reload_settings

# Stage-local alias —— 表达"该参数只关心 fetching 字段"的语义。
BiliEnv = _BiliSettings


__all__ = ["BiliEnv", "get_settings", "reload_settings"]
