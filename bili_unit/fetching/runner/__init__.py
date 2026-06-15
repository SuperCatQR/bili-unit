# runner — orchestrates fetch execution, retries, and progress tracking.
#
# Split into sub-modules for maintainability:
#   _item_ids.py    — item ID extraction (pure functions)
#   _endpoint.py    — _EndpointMixin._run_endpoint (uid-level single endpoint)
#   _item_fanout.py — _ItemFanoutMixin._run_item_endpoint / _process_single_item
#
# This module retains: orchestration (run_task, _run), helpers, and the Runner
# class that composes the mixins.
#
# fetch_endpoint is injected via Runner constructor (fetch_fn=).

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..._env import BiliSettings
from ..._logging import Progress
from .. import (
    AuthError,
    EndpointStatus,
    TaskResult,
    TaskStatus,
)
from .._bilibili_adapter import (
    fetch_endpoint as _real_fetch_endpoint,
)
from .._endpoint_catalog import ENDPOINTS, get_endpoint
from .._endpoint_spec import EndpointSpec
from .._store import FetchingStore
from ..auth import get_credential
from ..rate_limit import RateLimitController
from ._endpoint import _EndpointMixin
from ._item_fanout import _ItemFanoutMixin
from ._item_ids import _extract_item_ids, _extract_item_ids_multi  # noqa: F401

logger = logging.getLogger("bili.fetching.runner")

FetchEndpointFn = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner(_EndpointMixin, _ItemFanoutMixin):
    def __init__(
        self,
        store: FetchingStore,
        rate_limit: RateLimitController,
        settings: BiliSettings,
        stale_running_threshold_ms: int = 15 * 60 * 1000,
        fetch_fn: FetchEndpointFn | None = None,
    ) -> None:
        self._store = store
        self._rl = rate_limit
        self._settings = settings
        self._stale_threshold_ms = stale_running_threshold_ms
        self._fetch_fn = fetch_fn if fetch_fn is not None else _real_fetch_endpoint

    # -- public ------------------------------------------------------------

    async def run_task(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental",
    ) -> TaskResult:
        await self._bind_uid(uid)
        ep_names = endpoints or [ep.name for ep in ENDPOINTS]
        return await self._run(uid, ep_names, fresh=True, mode=mode)

    async def resume_task(
        self, uid: int, endpoints: list[str] | None = None,
    ) -> TaskResult:
        await self._bind_uid(uid)
        existing_eps = await self._list_endpoint_names()
        if not existing_eps and not endpoints:
            return TaskResult(uid=uid, status=TaskStatus.PENDING)
        ep_names = list(existing_eps)
        if endpoints is not None:
            for ep in endpoints:
                if ep not in existing_eps:
                    ep_names.append(ep)
            # init_task is idempotent on existing endpoints; we use it to seed
            # any new endpoints listed for this resume call.
            await self._store.init_task(ep_names)
            await self._store.update_task_status(TaskStatus.RUNNING.value)
        if not ep_names:
            return TaskResult(uid=uid, status=TaskStatus.PENDING)
        return await self._run(uid, ep_names, fresh=False)

    async def run_or_resume(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental",
    ) -> TaskResult:
        """Entry point for command: new task or resume existing.

        Handles idempotency internally — command need not check task state.

        Mode behaviour:
          incremental — SUCCESS task enters incremental scan (not skipped).
          full        — SUCCESS task triggers full re-fetch.
        """
        await self._bind_uid(uid)
        status_str = await self._store.get_task_status()
        if status_str is None:
            return await self.run_task(uid, endpoints, mode=mode)
        status = TaskStatus(status_str)

        if status == TaskStatus.RUNNING:
            updated_at = await self._fetch_task_updated_at()
            now_ms = int(time.time() * 1000)
            age_ms = now_ms - (updated_at or 0)
            if age_ms < self._stale_threshold_ms:
                logger.info(
                    "task_already_running",
                    extra={"uid": uid, "age_ms": age_ms},
                )
                return TaskResult(uid=uid, status=TaskStatus.RUNNING)
            # Stale: previous run died (SIGKILL / timeout / OOM) without writing
            # final status. Treat as PARTIAL so the resume path takes over.
            logger.warning(
                "task_stale_running_takeover",
                extra={
                    "uid": uid,
                    "age_ms": age_ms,
                    "threshold_ms": self._stale_threshold_ms,
                },
            )
            await self._store.update_task_status(TaskStatus.PARTIAL.value)
            status = TaskStatus.PARTIAL

        if status == TaskStatus.FAILED_PERMANENT:
            logger.info("task_failed_permanent_skip", extra={"uid": uid})
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)

        if status == TaskStatus.SUCCESS:
            if mode in ("incremental", "refresh"):
                logger.info("task_incremental_scan", extra={"uid": uid, "mode": mode})
                return await self._run(
                    uid, endpoints or [ep.name for ep in ENDPOINTS],
                    fresh=False, mode=mode,
                )
            elif mode == "full":
                logger.info("task_full_refetch", extra={"uid": uid})
                return await self.run_task(uid, endpoints, mode=mode)
            # fallback: skip
            return TaskResult(uid=uid, status=TaskStatus.SUCCESS)

        # PARTIAL / FAILED_RETRYABLE / FAILED_EXHAUSTED → resume
        if mode == "full":
            return await self.run_task(uid, endpoints, mode=mode)
        return await self.resume_task(uid, endpoints)

    # -- internal hub ------------------------------------------------------

    async def _run(
        self, uid: int, endpoints: list[str], fresh: bool, mode: str = "incremental",
    ) -> TaskResult:
        # auth — always required; fetching does not operate without credential
        try:
            credential = await get_credential()
        except AuthError as exc:
            await self._store.record_error(
                endpoint=None,
                error_type=type(exc).__name__,
                message=str(exc),
                retryable=False,
            )
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)

        # seed task + endpoint state (idempotent on existing rows)
        if fresh:
            # full / fresh run: clear any leftover progress for these endpoints
            # so we don't accidentally pick up stale cursors. We honour init_task's
            # idempotency by then explicitly resetting endpoint statuses.
            await self._store.init_task(endpoints)
            await self._store.update_task_status(TaskStatus.RUNNING.value)
            for ep_name in endpoints:
                await self._store.update_endpoint_state(
                    ep_name,
                    status=EndpointStatus.PENDING.value,
                    retry_count=0,
                )
        else:
            await self._store.init_task(endpoints)
            await self._store.update_task_status(TaskStatus.RUNNING.value)
            for ep_name in endpoints:
                state = await self._store.get_endpoint_state(ep_name)
                if state is None:
                    await self._store.update_endpoint_state(
                        ep_name,
                        status=EndpointStatus.PENDING.value,
                        retry_count=0,
                    )
                    continue
                ep_status = state.get("status")
                if ep_status == EndpointStatus.FAILED_EXHAUSTED.value:
                    await self._store.update_endpoint_state(
                        ep_name,
                        status=EndpointStatus.PENDING.value,
                        retry_count=0,
                    )
                elif ep_status == EndpointStatus.SUCCESS.value and mode in (
                    "incremental", "refresh",
                ):
                    # Reset endpoints in the current run list for re-scan.
                    await self._store.update_endpoint_state(
                        ep_name,
                        status=EndpointStatus.PENDING.value,
                        retry_count=state.get("retry_count", 0),
                    )

        # ---- Phase 1: uid-level endpoints (parallel) ----
        uid_tasks = []
        item_specs: list[tuple[str, EndpointSpec]] = []

        for ep_name in endpoints:
            state = await self._store.get_endpoint_state(ep_name)
            if state is None:
                continue
            if state.get("status") == EndpointStatus.SUCCESS.value:
                continue
            spec = get_endpoint(ep_name)
            if spec is None:
                err_id = await self._store.record_error(
                    endpoint=ep_name,
                    error_type="FetchingError",
                    message=f"unknown endpoint: {ep_name}",
                    retryable=False,
                )
                await self._store.update_endpoint_state(
                    ep_name,
                    status=EndpointStatus.FAILED_PERMANENT.value,
                    last_error_id=err_id,
                )
                continue

            if spec.kind == "uid":
                uid_tasks.append(
                    self._run_endpoint(uid, spec, ep_name, credential, mode),
                )
            elif spec.kind == "item":
                item_specs.append((ep_name, spec))
                # ensure source_endpoint is in the task
                if spec.source_endpoint:
                    src_state = await self._store.get_endpoint_state(
                        spec.source_endpoint,
                    )
                    if src_state is None:
                        # source not seeded → seed and run in Phase 1
                        await self._store.init_task([spec.source_endpoint])
                        src_spec = get_endpoint(spec.source_endpoint)
                        if src_spec is not None:
                            uid_tasks.append(
                                self._run_endpoint(
                                    uid, src_spec, spec.source_endpoint,
                                    credential, mode,
                                ),
                            )

        if uid_tasks:
            with Progress(
                total=len(uid_tasks),
                label=f"fetch uid={uid} endpoints",
            ) as bar:
                await self._gather_with_progress(uid_tasks, bar)

        # ---- Phase 2: item-level fan-out endpoints ----
        if item_specs:
            item_tasks = []
            for ep_name, spec in item_specs:
                state = await self._store.get_endpoint_state(ep_name)
                if state is None or state.get("status") == EndpointStatus.SUCCESS.value:
                    continue
                # check source endpoint status
                if spec.source_endpoint:
                    src_state = await self._store.get_endpoint_state(
                        spec.source_endpoint,
                    )
                    if (
                        src_state is None
                        or src_state.get("status") != EndpointStatus.SUCCESS.value
                    ):
                        err_id = await self._store.record_error(
                            endpoint=ep_name,
                            error_type="FetchingError",
                            message=(
                                f"source endpoint {spec.source_endpoint} not SUCCESS"
                            ),
                            retryable=False,
                        )
                        await self._store.update_endpoint_state(
                            ep_name,
                            status=EndpointStatus.FAILED_PERMANENT.value,
                            last_error_id=err_id,
                        )
                        logger.info(
                            "item_endpoint_source_failed",
                            extra={
                                "uid": uid, "endpoint": ep_name,
                                "source_endpoint": spec.source_endpoint,
                            },
                        )
                        continue
                item_tasks.append(
                    self._run_item_endpoint(uid, spec, credential, mode),
                )

            if item_tasks:
                with Progress(
                    total=len(item_tasks),
                    label=f"fetch uid={uid} items",
                ) as bar:
                    await self._gather_with_progress(item_tasks, bar)

        # final summary
        endpoint_statuses = await self._all_endpoint_statuses()
        if not endpoint_statuses:
            await self._store.update_task_status(TaskStatus.FAILED_PERMANENT.value)
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)
        final_task_status = self._derive_status_from_statuses(
            list(endpoint_statuses.values()),
        )
        await self._store.update_task_status(final_task_status.value)
        return TaskResult(
            uid=uid, status=final_task_status,
            endpoints=endpoint_statuses,
        )

    # -- helpers -----------------------------------------------------------

    async def _bind_uid(self, uid: int) -> None:
        """Hook for multi-uid proxy stores; the real FetchingStore ignores it."""
        bind = getattr(self._store, "_bind_uid", None)
        if bind is not None:
            res = bind(uid)
            if asyncio.iscoroutine(res):
                await res

    async def _list_endpoint_names(self) -> list[str]:
        rows = await self._store.ctx.main.fetch_all(
            "SELECT endpoint FROM fetch_endpoint_state ORDER BY endpoint",
        )
        return [r["endpoint"] for r in rows]

    async def _all_endpoint_statuses(self) -> dict[str, EndpointStatus]:
        rows = await self._store.ctx.main.fetch_all(
            "SELECT endpoint, status FROM fetch_endpoint_state ORDER BY endpoint",
        )
        out: dict[str, EndpointStatus] = {}
        for r in rows:
            try:
                out[r["endpoint"]] = EndpointStatus(r["status"])
            except ValueError:
                out[r["endpoint"]] = EndpointStatus.PENDING
        return out

    async def _fetch_task_updated_at(self) -> int | None:
        row = await self._store.ctx.main.fetch_one(
            "SELECT updated_at_ms FROM stage_task WHERE stage = 'fetching'",
        )
        if row is None:
            return None
        return row["updated_at_ms"]

    @staticmethod
    async def _gather_with_progress(coros: list, bar: Progress) -> None:
        """gather() variant that ticks ``bar`` once per coro completion.

        Wraps each coroutine in a small shim so we don't depend on
        ``asyncio.as_completed`` (which loses task identity on cancel).
        Exceptions are swallowed at this layer — endpoints already record
        their own errors via ``self._store.record_error``; this matches the
        prior ``return_exceptions=True`` behaviour.
        """
        async def _wrap(coro):
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 — preserve gather semantics
                return exc
            finally:
                bar.update(1)

        await asyncio.gather(*[_wrap(c) for c in coros])

    @staticmethod
    def _derive_status_from_statuses(
        statuses: list[EndpointStatus],
    ) -> TaskStatus:
        if not statuses:
            return TaskStatus.PENDING
        if all(s in (EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM) for s in statuses):
            return TaskStatus.SUCCESS
        has_perm_fail = any(
            s == EndpointStatus.FAILED_PERMANENT for s in statuses
        )
        has_exhausted = any(
            s == EndpointStatus.FAILED_EXHAUSTED for s in statuses
        )
        has_retryable = any(
            s == EndpointStatus.FAILED_RETRYABLE for s in statuses
        )
        has_success = any(
            s in (EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM) for s in statuses
        )

        if has_perm_fail:
            return TaskStatus.PARTIAL if has_success else TaskStatus.FAILED_PERMANENT
        if has_exhausted:
            return TaskStatus.PARTIAL if has_success else TaskStatus.FAILED_EXHAUSTED
        if has_retryable:
            return TaskStatus.FAILED_RETRYABLE
        return TaskStatus.PARTIAL

    # -- legacy-compatible helper kept for tests -----------------------------

    @staticmethod
    def _derive_status(tv) -> TaskStatus:
        """Compatibility shim — derive task status from a legacy TaskValue.

        New code uses :meth:`_derive_status_from_statuses` directly.
        """
        statuses = [v.status for v in tv.endpoints.values()]
        return Runner._derive_status_from_statuses(statuses)
