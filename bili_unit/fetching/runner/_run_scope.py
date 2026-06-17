"""Fetching run scope: endpoint set, resume decision, and task state setup."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .. import EndpointStatus, TaskResult, TaskStatus
from .._endpoint_catalog import ENDPOINTS
from .._store import FetchingStore

logger = logging.getLogger("bili.fetching.runner")


@dataclass(frozen=True)
class FetchRunScope:
    """A concrete fetching invocation after task-state decisions are resolved."""

    uid: int
    endpoints: list[str]
    fresh: bool
    mode: str = "incremental"


@dataclass(frozen=True)
class FetchRunDecision:
    """Decision made from current task state.

    ``scope`` is present when the runner should execute work; ``result`` is
    present when the task can return immediately.
    """

    scope: FetchRunScope | None = None
    result: TaskResult | None = None


def all_endpoint_names() -> list[str]:
    return [ep.name for ep in ENDPOINTS]


class FetchRunPlanner:
    """Resolve fetching mode / resume semantics into one run scope."""

    def __init__(
        self,
        store: FetchingStore,
        *,
        stale_running_threshold_ms: int,
        now_seconds: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._stale_running_threshold_ms = stale_running_threshold_ms
        self._now_seconds = now_seconds if now_seconds is not None else time.time

    async def new_scope(
        self,
        uid: int,
        endpoints: list[str] | None,
        *,
        mode: str,
    ) -> FetchRunScope:
        return FetchRunScope(
            uid=uid,
            endpoints=list(endpoints) if endpoints is not None else all_endpoint_names(),
            fresh=True,
            mode=mode,
        )

    async def resume_scope(
        self,
        uid: int,
        endpoints: list[str] | None,
        *,
        mode: str = "incremental",
    ) -> FetchRunDecision:
        existing_eps = await self._store.list_endpoint_names()
        if not existing_eps and not endpoints:
            return FetchRunDecision(
                result=TaskResult(uid=uid, status=TaskStatus.PENDING),
            )
        ep_names = list(endpoints) if endpoints is not None else list(existing_eps)
        if not ep_names:
            return FetchRunDecision(
                result=TaskResult(uid=uid, status=TaskStatus.PENDING),
            )
        return FetchRunDecision(
            scope=FetchRunScope(
                uid=uid,
                endpoints=ep_names,
                fresh=False,
                mode=mode,
            ),
        )

    async def run_or_resume_scope(
        self,
        uid: int,
        endpoints: list[str] | None,
        *,
        mode: str,
    ) -> FetchRunDecision:
        status_str = await self._store.get_task_status()
        if status_str is None:
            return FetchRunDecision(
                scope=await self.new_scope(uid, endpoints, mode=mode),
            )
        status = TaskStatus(status_str)

        if status == TaskStatus.RUNNING:
            updated_at = await self._store.get_task_updated_at()
            now_ms = int(self._now_seconds() * 1000)
            age_ms = now_ms - (updated_at or 0)
            if age_ms < self._stale_running_threshold_ms:
                logger.info(
                    "task_already_running",
                    extra={"uid": uid, "age_ms": age_ms},
                )
                return FetchRunDecision(
                    result=TaskResult(uid=uid, status=TaskStatus.RUNNING),
                )
            logger.warning(
                "task_stale_running_takeover",
                extra={
                    "uid": uid,
                    "age_ms": age_ms,
                    "threshold_ms": self._stale_running_threshold_ms,
                },
            )
            await self._store.update_task_status(TaskStatus.PARTIAL.value)
            status = TaskStatus.PARTIAL

        if status == TaskStatus.FAILED_PERMANENT:
            logger.info("task_failed_permanent_skip", extra={"uid": uid})
            return FetchRunDecision(
                result=TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT),
            )

        if status == TaskStatus.SUCCESS:
            if mode in ("incremental", "refresh"):
                logger.info("task_incremental_scan", extra={"uid": uid, "mode": mode})
                return FetchRunDecision(
                    scope=FetchRunScope(
                        uid=uid,
                        endpoints=(
                            list(endpoints)
                            if endpoints is not None
                            else all_endpoint_names()
                        ),
                        fresh=False,
                        mode=mode,
                    ),
                )
            if mode == "full":
                logger.info("task_full_refetch", extra={"uid": uid})
                return FetchRunDecision(
                    scope=await self.new_scope(uid, endpoints, mode=mode),
                )
            return FetchRunDecision(
                result=TaskResult(uid=uid, status=TaskStatus.SUCCESS),
            )

        if mode == "full":
            return FetchRunDecision(
                scope=await self.new_scope(uid, endpoints, mode=mode),
            )
        return await self.resume_scope(uid, endpoints, mode=mode)


async def prepare_scope(store: FetchingStore, scope: FetchRunScope) -> None:
    await store.prepare_task_run(
        scope.endpoints,
        fresh=scope.fresh,
        mode=scope.mode,
    )


async def endpoint_statuses(
    store: FetchingStore,
    endpoints: list[str] | None = None,
) -> dict[str, EndpointStatus]:
    rows = await store.list_endpoint_statuses(endpoints)
    out: dict[str, EndpointStatus] = {}
    for endpoint, status in rows.items():
        try:
            out[endpoint] = EndpointStatus(status)
        except ValueError:
            out[endpoint] = EndpointStatus.PENDING
    return out
