# command — fetching write-side entry point.

import logging

from .._env import BiliSettings
from . import CommandResult
from .data import DataStore
from .error import ErrorStore
from .rate_limit import RateLimitController
from .runner import FetchEndpointFn, Runner

logger = logging.getLogger("bili.fetching.command")


class Command:
    """External write-side entry.

    Does NOT provide data/error read access.
    Does NOT call client directly.
    """

    def __init__(
        self,
        data: DataStore,
        error: ErrorStore,
        rate_limit: RateLimitController,
        settings: BiliSettings,
        stale_running_threshold_ms: int = 15 * 60 * 1000,
        fetch_fn: FetchEndpointFn | None = None,
    ) -> None:
        self._data = data
        self._error = error
        self._rl = rate_limit
        self._settings = settings
        self._runner = Runner(
            data, error, rate_limit, settings,
            stale_running_threshold_ms=stale_running_threshold_ms,
            fetch_fn=fetch_fn,
        )

    async def fetch_uid(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental"
    ) -> CommandResult:
        """Trigger fetching for a uid.

        Delegates idempotency to runner.run_or_resume().

        Args:
            mode: "incremental" (default) scans for new content;
                  "full" re-fetches everything from scratch.
        """
        logger.info("command_received", extra={"uid": uid, "mode": mode})
        result = await self._runner.run_or_resume(uid, endpoints, mode=mode)
        return CommandResult(uid=uid, status=result.status)

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """Delete all fetching state for a uid. Returns counts."""
        data_count = await self._data.delete_by_uid_prefix(uid)
        error_count = await self._error.delete_by_uid(uid)
        return {"data": data_count, "errors": error_count}

    async def close(self) -> None:
        """Close underlying stores."""
        await self._data.close()
        await self._error.close()
