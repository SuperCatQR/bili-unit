# command — processing write-side entry.
#
# Per docs/design/processing.md §11.1: ProcessingCommand exposes only
# process_uid(); MVP does not provide retry_failed() (decision §19).
#
# Boundaries (docs/structure/bili.md §8):
#   - command 不直接调用 transform / audio
#   - command 不写 raw / temp / data（runner does that）
#   - command 不提供 data / error 读取（that's query）

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from . import ProcessingCommandResult, ProcessingTaskStatus
from .runner import ProcessingRunner

if TYPE_CHECKING:
    from ..fetching.query import Query as FetchingQuery
    from ..parsing.query import ParsingQuery
    from .audio._asr_backend import ASRBackend
    from .data import ProcessingDataStore
    from .env import ProcessingEnv
    from .error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.command")


class ProcessingCommand:
    """Bili processing write-side entry."""

    def __init__(
        self,
        data: ProcessingDataStore,
        error: ProcessingErrorStore,
        temp_dir: str,
        fetching_query: FetchingQuery,
        settings: ProcessingEnv,
        asr_backend: ASRBackend | None = None,
        fetching_close: Callable[[], Awaitable[None]] | None = None,
        parsing_query: ParsingQuery | None = None,
    ) -> None:
        self._data = data
        self._error = error
        self._fetching_close = fetching_close
        self._asr_backend = asr_backend
        self._runner = ProcessingRunner(
            data=data,
            error=error,
            temp_dir=temp_dir,
            fetching_query=fetching_query,
            settings=settings,
            asr_backend=asr_backend,
            parsing_query=parsing_query,
        )

    async def process_uid(
        self,
        uid: int,
        pipelines: list[str] | None = None,
        item_types: list[str] | None = None,
        mode: str = "incremental",
    ) -> ProcessingCommandResult:
        """Trigger processing for a uid.

        Args:
            pipelines: subset of {"transform", "audio"}; default all.
            item_types: subset of registered transform item_types; default all.
            mode: "incremental" (default) | "full".
        """
        logger.info(
            "command_received",
            extra={"uid": uid, "mode": mode, "pipelines": pipelines,
                   "item_types": item_types},
        )
        status: ProcessingTaskStatus = await self._runner.run(
            uid, pipelines=pipelines, item_types=item_types, mode=mode,
        )
        return ProcessingCommandResult(uid=uid, status=status)

    async def close(self) -> None:
        await self._data.close()
        await self._error.close()
        if self._asr_backend is not None:
            await self._asr_backend.close()
        if self._fetching_close is not None:
            await self._fetching_close()
