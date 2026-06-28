# command — fetching write-side entry point.

import logging
from typing import TYPE_CHECKING

from .._db import UidContext
from .._env import BiliSettings
from . import CommandResult
from ._bilibili_adapter import FetchPageResult
from ._store import FetchingStore
from .rate_limit import RateLimitController
from .runner import FetchEndpointFn, Runner, default_progress_factory

if TYPE_CHECKING:
    from .worker_client import WorkerClient

logger = logging.getLogger("bili.fetching.command")


class Command:
    """External write-side entry.

    Phase 3 contract:
      * Holds only cross-request services (settings, rate limit, worker). The store
        layer is request-scoped — every ``fetch_uid`` opens its own
        ``UidContext`` + ``FetchingStore``, then closes it on exit.
      * ``delete_uid`` is a no-op; the unit-level ``BiliCommand.delete_uid``
        deletes on-disk files directly.
      * F2: when ``worker`` is provided, fetch_page / fetch_item are routed
        through the bili-worker subprocess via IPC.
    """

    def __init__(
        self,
        settings: BiliSettings,
        rate_limit: RateLimitController,
        *,
        stale_running_threshold_ms: int = 15 * 60 * 1000,
        fetch_fn: FetchEndpointFn | None = None,
        worker: "WorkerClient | None" = None,
    ) -> None:
        self._settings = settings
        self._rl = rate_limit
        self._stale_running_threshold_ms = stale_running_threshold_ms
        self._fetch_fn = fetch_fn
        self._worker = worker

    async def fetch_uid(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental",
    ) -> CommandResult:
        """Trigger fetching for a uid.

        Opens a per-uid ``UidContext`` for the duration of the run and closes
        it before returning.
        """
        logger.info("command_received", extra={"uid": uid, "mode": mode})
        ctx = UidContext(uid, self._settings.bili_db_dir)
        await ctx.open()
        try:
            from ..observability import RunContext, RunReporter, SqliteSink

            store = FetchingStore(ctx)
            run_context = RunContext.create(
                uid=uid,
                command="fetch",
                args={
                    "mode": mode,
                    "endpoints": endpoints,
                },
            )
            # F2: if worker is available, route fetch_page / fetch_item through IPC.
            # The fetch_fn wraps worker.fetch_page to return a FetchPageResult
            # compatible with the existing runner (runner 零改动 for uid-level).
            effective_fetch_fn = self._fetch_fn
            effective_item_fn = None
            if effective_fetch_fn is None and self._worker is not None:
                _w = self._worker
                async def _worker_fetch_fn(uid, spec, credential, params, timeout=30.0):
                    raw_payload = await _w.fetch_page(
                        uid=uid,
                        endpoint=spec.name,
                        credential_ref=self._worker.credential_ref,
                        request_params=params,
                        timeout=timeout,
                    )
                    return FetchPageResult(
                        uid=uid, endpoint=spec.name, raw_payload=raw_payload,
                    )
                effective_fetch_fn = _worker_fetch_fn

                async def _worker_item_fn(item_id, spec, credential, parent_uid=None, timeout=30.0):
                    return await _w.fetch_item(
                        item_id=item_id,
                        endpoint=spec.name,
                        credential_ref=self._worker.credential_ref,
                        extra={"_uid": parent_uid} if parent_uid is not None else None,
                        timeout=timeout,
                    )
                effective_item_fn = _worker_item_fn

            runner = Runner(
                store, self._rl, self._settings,
                stale_running_threshold_ms=self._stale_running_threshold_ms,
                fetch_fn=effective_fetch_fn,
                item_fn=effective_item_fn,
                reporter=RunReporter(run_context, SqliteSink(ctx.conn)),
                progress_factory=default_progress_factory,
            )
            result = await runner.run_or_resume(uid, endpoints, mode=mode)
        finally:
            await ctx.close()
        return CommandResult(uid=uid, status=result.status, run_id=run_context.run_id)

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """No-op: the unit-level ``BiliCommand.delete_uid`` does file IO directly."""
        return {}

    async def close(self) -> None:
        """Shut down the worker if one was spawned."""
        if self._worker is not None:
            await self._worker.shutdown()
        return None
