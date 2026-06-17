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
from .._endpoint_catalog import get_endpoint
from .._endpoint_spec import EndpointSpec
from .._store import FetchingStore
from ..auth import get_credential
from ..rate_limit import RateLimitController
from ._endpoint import _EndpointMixin
from ._item_fanout import _ItemFanoutMixin
from ._item_ids import _extract_item_ids, _extract_item_ids_multi  # noqa: F401
from ._run_scope import (
    FetchRunPlanner,
    FetchRunScope,
    prepare_scope,
)
from ._run_scope import (
    endpoint_statuses as _load_endpoint_statuses,
)

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
        self._fetch_fn = fetch_fn if fetch_fn is not None else _real_fetch_endpoint
        self._run_planner = FetchRunPlanner(
            store,
            stale_running_threshold_ms=stale_running_threshold_ms,
            now_seconds=lambda: time.time(),
        )

    # -- public ------------------------------------------------------------

    async def run_task(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental",
    ) -> TaskResult:
        await self._bind_uid(uid)
        scope = await self._run_planner.new_scope(uid, endpoints, mode=mode)
        return await self._run(scope)

    async def resume_task(
        self, uid: int, endpoints: list[str] | None = None,
    ) -> TaskResult:
        await self._bind_uid(uid)
        decision = await self._run_planner.resume_scope(uid, endpoints)
        if decision.result is not None:
            return decision.result
        assert decision.scope is not None
        return await self._run(decision.scope)

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
        decision = await self._run_planner.run_or_resume_scope(
            uid, endpoints, mode=mode,
        )
        if decision.result is not None:
            return decision.result
        assert decision.scope is not None
        return await self._run(decision.scope)

    # -- internal hub ------------------------------------------------------

    async def _run(self, scope: FetchRunScope) -> TaskResult:
        uid = scope.uid
        endpoints = scope.endpoints
        mode = scope.mode
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

        await prepare_scope(self._store, scope)

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
                    self._run_item_endpoint(
                        uid, spec, credential, mode, show_progress=False,
                    ),
                )

            if item_tasks:
                with Progress(
                    total=len(item_tasks),
                    label=f"fetch uid={uid} items",
                ) as bar:
                    await self._gather_with_progress(item_tasks, bar)

        # final summary
        endpoint_statuses = await _load_endpoint_statuses(self._store, endpoints)
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
