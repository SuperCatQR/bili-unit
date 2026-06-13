# bili_unit.query — unified read-side entry across stages.
#
# Per docs/structure/bili.md §10, the bili unit's outward read entry is the
# `query/` package. Its job is to expose read-only views over each stage's
# data / error stores.
#
# Boundaries (docs/structure/bili.md §8):
#   - query 不暴露 data / error 内部存储结构
#   - query 不做跨源归一化语义处理
#   - query 不触发写侧流程
#   - query 不读取 raw / temp

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fetching.query import Query as _FetchingQuery

if TYPE_CHECKING:
    from ..parsing.query import ParsingQuery as _ParsingQuery
    from ..processing.query import ProcessingQuery as _ProcessingQuery


class BiliQuery:
    """Bili unit的统一只读入口；持有各阶段 query 实例。"""

    def __init__(
        self,
        fetching: _FetchingQuery,
        parsing: _ParsingQuery | None = None,
        processing: _ProcessingQuery | None = None,
    ) -> None:
        self._fetching = fetching
        self._parsing = parsing
        self._processing = processing

    @property
    def fetching(self) -> _FetchingQuery:
        return self._fetching

    @property
    def parsing(self) -> _ParsingQuery:
        if self._parsing is None:
            raise RuntimeError("parsing query was not assembled")
        return self._parsing

    @property
    def processing(self) -> _ProcessingQuery:
        if self._processing is None:
            raise RuntimeError("processing query was not assembled")
        return self._processing


__all__ = ["BiliQuery"]
