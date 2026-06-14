# command — processing write-side entry.
#
# ProcessingCommand exposes only process_uid(); there is no retry_failed()
# — failed work items are retried by re-invoking process_uid() in
# incremental mode.
#
# Boundaries (docs/structure/bili.md §8):
#   - command 不直接调用 audio
#   - command 不写 raw / temp / data（runner does that）
#   - command 不提供 data / error 读取（that's query）

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from . import ProcessingCommandResult, ProcessingTaskStatus
from .runner import ConvertFn, CredentialProvider, DownloaderFactory, ProcessingRunner

if TYPE_CHECKING:
    from ..fetching.protocols import FetchingReadView
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
        fetching_query: FetchingReadView,
        settings: ProcessingEnv,
        asr_backend: ASRBackend | None = None,
        credential_provider: CredentialProvider | None = None,
        downloader_factory: DownloaderFactory | None = None,
        convert_fn: ConvertFn | None = None,
    ) -> None:
        self._data = data
        self._error = error
        self._asr_backend = asr_backend
        self._temp_dir = temp_dir
        self._settings = settings
        self._runner = ProcessingRunner(
            data=data,
            error=error,
            temp_dir=temp_dir,
            fetching_query=fetching_query,
            settings=settings,
            asr_backend=asr_backend,
            credential_provider=credential_provider,
            downloader_factory=downloader_factory,
            convert_fn=convert_fn,
        )

    async def process_uid(
        self,
        uid: int,
        mode: str = "incremental",
    ) -> ProcessingCommandResult:
        """Trigger processing for a uid.

        Args:
            mode: "incremental" (default) | "full".
        """
        logger.info(
            "command_received",
            extra={"uid": uid, "mode": mode},
        )
        status: ProcessingTaskStatus = await self._runner.run(uid, mode=mode)
        return ProcessingCommandResult(uid=uid, status=status)

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """Delete all processing state for a uid. Returns counts."""
        data_count = await self._data.delete_by_uid_prefix(uid)
        error_count = await self._error.delete_by_uid(uid)
        # Remove temp directory for this uid
        temp_uid_dir = Path(self._temp_dir) / str(uid)
        temp_existed = temp_uid_dir.exists()
        if temp_existed:
            shutil.rmtree(temp_uid_dir, ignore_errors=True)
        # Remove ASR cache directory for this uid (layout: {asr_cache_dir}/{uid}/)
        asr_cache_uid_dir = Path(self._settings.bili_processing_asr_cache_dir) / str(uid)
        asr_cache_existed = asr_cache_uid_dir.exists()
        if asr_cache_existed:
            shutil.rmtree(asr_cache_uid_dir, ignore_errors=True)
        return {
            "data": data_count,
            "errors": error_count,
            "temp_removed": int(temp_existed),
            "asr_cache_removed": int(asr_cache_existed),
        }

    async def close(self) -> None:
        await self._data.close()
        await self._error.close()
        if self._asr_backend is not None:
            await self._asr_backend.close()
