# bili_unit.command — unified write-side entry across stages.
#
# Per docs/structure/bili.md §10, the bili unit's outward write entry is the
# `command/` package. Its job is to编排 fetching / processing / cleansing
# stages (each owns its own internal command). Today fetching + processing
# are wired; cleansing comes later.
#
# Boundaries (docs/structure/bili.md §8):
#   - command 不直接调用 client / transform / audio
#   - command 不写 raw / temp / data
#   - command 不提供 data / error 读取（那是 query 的事）

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fetching import CommandResult
from ..fetching.command import Command as _FetchingCommand

if TYPE_CHECKING:
    from ..processing import ProcessingCommandResult
    from ..processing.command import ProcessingCommand as _ProcessingCommand


class BiliCommand:
    """Bili unit的统一写侧入口；编排各阶段 command。"""

    def __init__(
        self,
        fetching: _FetchingCommand,
        processing: _ProcessingCommand | None = None,
    ) -> None:
        self._fetching = fetching
        self._processing = processing

    # -- fetching stage ----------------------------------------------------

    async def fetch(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        """触发 fetching 抓取流水线。"""
        return await self._fetching.fetch_uid(uid, endpoints, mode=mode)

    # -- processing stage --------------------------------------------------

    async def process(
        self,
        uid: int,
        pipelines: list[str] | None = None,
        item_types: list[str] | None = None,
        mode: str = "incremental",
    ) -> ProcessingCommandResult:
        """触发 processing 处理流水线。"""
        if self._processing is None:
            raise RuntimeError("processing command was not assembled")
        return await self._processing.process_uid(
            uid, pipelines=pipelines, item_types=item_types, mode=mode,
        )

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        if self._processing is not None:
            await self._processing.close()
        await self._fetching.close()


__all__ = ["BiliCommand"]
