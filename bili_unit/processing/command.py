# command — processing write-side entry.
#
# ProcessingCommand exposes only process_uid(); there is no retry_failed()
# — failed work items are retried by re-invoking process_uid() in
# incremental mode.
#
# Phase 3.3 contract:
#   * No data / error / fetching_query / parsing_query parameters; the
#     command holds only stable, cross-uid state (settings, asr_backend,
#     credential_provider, optional downloader / convert injectors).
#   * Each ``asr_uid(uid, ...)`` call opens its own main-DB ``UidContext`` +
#     ``ProcessingStore`` / ``ParsingStore`` and tears them down on return.
#   * ``delete_uid`` is a no-op (returns ``{}``); ``BiliCommand.delete_uid``
#     handles the file-IO removal of the per-uid databases and workdir.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import ProcessingCommandResult
from .runner import (
    ConvertFn,
    CredentialProvider,
    DownloaderFactory,
    ProcessingRunner,
)

if TYPE_CHECKING:
    from .._env import BiliSettings
    from .audio._asr_backend import ASRBackend

logger = logging.getLogger("bili.processing.command")


class ProcessingCommand:
    """Bili processing write-side entry."""

    def __init__(
        self,
        settings: BiliSettings,
        *,
        asr_backend: ASRBackend | None = None,
        credential_provider: CredentialProvider | None = None,
        downloader_factory: DownloaderFactory | None = None,
        convert_fn: ConvertFn | None = None,
    ) -> None:
        self._settings = settings
        self._asr_backend = asr_backend
        self._credential_provider = credential_provider
        self._downloader_factory = downloader_factory
        self._convert_fn = convert_fn
        # The runner is stateless across process_uid calls; we instantiate it
        # once with the cross-uid services and pass the per-uid stores into
        # ``run()``.
        self._runner = ProcessingRunner(
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
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> ProcessingCommandResult:
        """Backward-compatible alias for :meth:`asr_uid`."""
        return await self.asr_uid(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
            max_audio_seconds=max_audio_seconds,
            max_audio_tokens=max_audio_tokens,
        )

    async def asr_uid(
        self,
        uid: int,
        mode: str = "incremental",
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> ProcessingCommandResult:
        """Trigger ASR for a uid.

        Opens a fresh main-DB :class:`UidContext`, binds
        :class:`ProcessingStore` / :class:`ParsingStore` to it, and dispatches
        to the runner. The context is closed on return regardless of outcome.

        Args:
            mode: "incremental" (default) | "full".
            limit: cap discovered bvids to the first N after other filters.
            only_bvids: restrict processing to this explicit set of bvids.
            retry_failed_only: only re-process bvids whose existing status is
                FAILED (incremental-mode extension).
            dry_run: discover candidates and write task / progress markers,
                but skip worker dispatch. Status is SUCCESS; the candidate
                list is returned via ``ProcessingCommandResult.dry_run_candidates``.
            max_audio_seconds / max_audio_tokens: optional pre-dispatch budget
                caps. When exceeded, dispatch is skipped and the result reports
                ``budget_exceeded``.
        """
        from .._db import UidContext
        from ..parsing._store import ParsingStore
        from ._store import ProcessingStore

        logger.info(
            "asr_command_received",
            extra={
                "uid": uid,
                "mode": mode,
                "limit": limit,
                "only_bvids": only_bvids,
                "retry_failed_only": retry_failed_only,
                "dry_run": dry_run,
                "max_audio_seconds": max_audio_seconds,
                "max_audio_tokens": max_audio_tokens,
            },
        )

        ctx = UidContext(uid, self._settings.bili_db_dir)
        await ctx.open(raw=False)
        try:
            proc_store = ProcessingStore(ctx)
            parse_store = ParsingStore(ctx)
            status, candidates, estimate, budget_exceeded = await self._runner.run(
                uid,
                proc_store=proc_store,
                parse_store=parse_store,
                mode=mode,
                limit=limit,
                only_bvids=only_bvids,
                retry_failed_only=retry_failed_only,
                dry_run=dry_run,
                max_audio_seconds=max_audio_seconds,
                max_audio_tokens=max_audio_tokens,
            )
        finally:
            await ctx.close()
        return ProcessingCommandResult(
            uid=uid,
            status=status,
            dry_run_candidates=candidates if dry_run or budget_exceeded else None,
            estimate=estimate,
            budget_exceeded=budget_exceeded or None,
        )

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """No-op in Phase 3.

        ``BiliCommand.delete_uid`` removes ``{uid}.db`` / ``{uid}.raw.db``
        / ``{db_dir}/{uid}/`` directly. The processing stage's per-uid
        files (audio temp + ASR cache) live OUTSIDE that workdir and are
        not cleaned up by this stage today; see the open question in the
        Phase 3.3 deliverable.
        """
        return {}

    async def close(self) -> None:
        """Close cross-uid resources held by the command (asr_backend HTTP
        session, etc.).

        Per-uid contexts are opened and closed inside :meth:`process_uid`;
        nothing context-scoped survives this call.
        """
        if self._asr_backend is not None:
            await self._asr_backend.close()
