# transform — 纯计算转换模块。
#
# Per docs/design/processing.md §6:
#   - 输入：raw_payload dict（从 fetching.query 读取）
#   - 输出：result dict（写入 processing data store）
#   - 无外部调用（无 HTTP / 文件 I/O / ASR）
#   - 确定性：相同输入 → 相同输出
#   - 可独立测试

from . import _base, articles, dynamics, opus, user_profile, video_metadata
from ._base import TransformHandler, WorkItem
from ._registry import HANDLERS, get_handler

__all__ = [
    "HANDLERS",
    "TransformHandler",
    "WorkItem",
    "_base",
    "articles",
    "dynamics",
    "get_handler",
    "opus",
    "user_profile",
    "video_metadata",
]
