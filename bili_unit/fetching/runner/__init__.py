# runner 鈥?orchestrates fetch execution, retries, and progress tracking.
#
# Split into sub-modules for maintainability:
#   _item_ids.py    鈥?item ID extraction (pure functions)
#   _endpoint.py    鈥?_EndpointMixin._run_endpoint (uid-level single endpoint)
#   _item_fanout.py 鈥?_ItemFanoutMixin._run_item_endpoint / _process_single_item
#
# This module retains: orchestration (run_task, _run), helpers, and the Runner
# class that composes the mixins.
#
# fetch_endpoint is injected via Runner constructor (fetch_fn=).

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..._env import BiliSettings
from ..._progress import (
    ProgressFactory,
    default_progress_factory,
    gather_with_progress,
)
from ...observability import RunReporter, RunStatus
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
        reporter: RunReporter | None = None,
        progress_factory: ProgressFactory | None = None,
    ) -> None:
        self._store = store
        self._rl = rate_limit
        self._settings = settings
        self._fetch_fn = fetch_fn if fetch_fn is not None else _real_fetch_endpoint
        self._reporter = reporter
        self._progress_factory = progress_factory or default_progress_factory
        self._run_planner = FetchRunPlanner(
            store,
            stale_running_threshold_ms=stale_running_threshold_ms,
            now_seconds=lambda: time.time(),
        )

    # -- public ------------------------------------------------------------

    async def run_task(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> TaskResult:
        return await self._run_with_reporting(
            uid,
            endpoints,
            mode=mode,
            scope_loader=lambda: self._run_planner.new_scope(
                uid,
                endpoints,
                mode=mode,
            ),
            scope_result=lambda scope: self._run(scope),
        )

    async def resume_task(
        self,
        uid: int,
        endpoints: list[str] | None = None,
    ) -> TaskResult:
        return await self._run_with_reporting(
            uid,
            endpoints,
            mode="resume",
            scope_loader=lambda: self._run_planner.resume_scope(uid, endpoints),
            scope_result=self._result_from_scope_decision,
        )

    async def run_or_resume(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> TaskResult:
        """Entry point for command: new task or resume existing.

        Handles idempotency internally 鈥?command need not check task state.

        Mode behaviour:
          incremental 鈥?SUCCESS task enters incremental scan (not skipped).
          full        鈥?SUCCESS task triggers full re-fetch.
        """
        return await self._run_with_reporting(
            uid,
            endpoints,
            mode=mode,
            scope_loader=lambda: self._run_planner.run_or_resume_scope(
                uid,
                endpoints,
                mode=mode,
            ),
            scope_result=self._result_from_scope_decision,
        )

    # -- internal hub ------------------------------------------------------

    async def _run(self, scope: FetchRunScope) -> TaskResult:
        uid = scope.uid
        endpoints = scope.endpoints
        mode = scope.mode

        await prepare_scope(self._store, scope)
        specs_by_name = {ep_name: get_endpoint(ep_name) for ep_name in endpoints}
        for spec in list(specs_by_name.values()):
            if spec is None or spec.kind != "item" or not spec.source_endpoint:
                continue
            specs_by_name.setdefault(
                spec.source_endpoint,
                get_endpoint(spec.source_endpoint),
            )
        credential = await self._resolve_credential(uid, specs_by_name)

        # ---- Phase 1: uid-level endpoints (parallel) ----
        uid_tasks = []
        item_specs: list[tuple[str, EndpointSpec]] = []

        for ep_name in endpoints:
            state = await self._store.get_endpoint_state(ep_name)
            if state is None:
                continue
            if state.get("status") == EndpointStatus.SUCCESS.value:
                continue
            spec = specs_by_name.get(ep_name)
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

            if spec.credential_required and credential is None:
                err_id = await self._store.record_error(
                    endpoint=ep_name,
                    error_type="AuthError",
                    message="credential required for endpoint",
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
                        # source not seeded 鈫?seed and run in Phase 1
                        await self._store.init_task([spec.source_endpoint])
                        src_spec = specs_by_name.get(spec.source_endpoint)
                        if src_spec is None:
                            src_spec = get_endpoint(spec.source_endpoint)
                            specs_by_name[spec.source_endpoint] = src_spec
                        if src_spec is not None:
                            if src_spec.credential_required and credential is None:
                                err_id = await self._store.record_error(
                                    endpoint=spec.source_endpoint,
                                    error_type="AuthError",
                                    message="credential required for endpoint",
                                    retryable=False,
                                )
                                await self._store.update_endpoint_state(
                                    spec.source_endpoint,
                                    status=EndpointStatus.FAILED_PERMANENT.value,
                                    last_error_id=err_id,
                                )
                                continue
                            uid_tasks.append(
                                self._run_endpoint(
                                    uid,
                                    src_spec,
                                    spec.source_endpoint,
                                    credential,
                                    mode,
                                ),
                            )

        if uid_tasks:
            await gather_with_progress(
                uid_tasks,
                total=len(uid_tasks),
                label=f"fetch uid={uid} endpoints",
                progress_factory=self._progress_factory,
            )

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
                    if src_state is None or src_state.get("status") != EndpointStatus.SUCCESS.value:
                        err_id = await self._store.record_error(
                            endpoint=ep_name,
                            error_type="FetchingError",
                            message=(f"source endpoint {spec.source_endpoint} not SUCCESS"),
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
                                "uid": uid,
                                "endpoint": ep_name,
                                "source_endpoint": spec.source_endpoint,
                            },
                        )
                        if self._reporter is not None:
                            await self._reporter.emit(
                                "fetch.endpoint.source_failed",
                                stage="fetching",
                                level="ERROR",
                                endpoint=ep_name,
                                data={"source_endpoint": spec.source_endpoint},
                            )
                        continue
                item_tasks.append(
                    self._run_item_endpoint(
                        uid,
                        spec,
                        credential,
                        mode,
                        show_progress=False,
                    ),
                )

            if item_tasks:
                await gather_with_progress(
                    item_tasks,
                    total=len(item_tasks),
                    label=f"fetch uid={uid} items",
                    progress_factory=self._progress_factory,
                )

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
            uid=uid,
            status=final_task_status,
            endpoints=endpoint_statuses,
        )

    # -- helpers -----------------------------------------------------------

    async def _resolve_credential(
        self,
        uid: int,
        specs_by_name: dict[str, EndpointSpec | None],
    ) -> Any | None:
        """Load credential only when the requested endpoint set needs one.

        Public Bilibili endpoints can run without cookies. Credential-required
        endpoints are marked individually below instead of failing the whole
        fetching run before public endpoints get a chance to complete.
        """
        needs_credential = any(spec is not None and spec.credential_required for spec in specs_by_name.values())
        if not needs_credential:
            return None
        try:
            return await get_credential()
        except AuthError as exc:
            await self._store.record_error(
                endpoint=None,
                error_type=type(exc).__name__,
                message=str(exc),
                retryable=False,
            )
            logger.info(
                "credential_unavailable",
                extra={
                    "uid": uid,
                    "credential_required_endpoints": [
                        ep_name
                        for ep_name, spec in specs_by_name.items()
                        if spec is not None and spec.credential_required
                    ],
                },
            )
            return None

    async def _run_with_reporting(
        self,
        uid: int,
        endpoints: list[str] | None,
        *,
        mode: str,
        scope_loader,
        scope_result,
    ) -> TaskResult:
        await self._bind_uid(uid)
        reporter = self._reporter
        if reporter is not None:
            await reporter.start()
            await reporter.emit(
                "fetch.run.started",
                stage="fetching",
                data={"mode": mode, "endpoints": endpoints},
            )
        try:
            loaded = await scope_loader()
            result_or_awaitable = scope_result(loaded)
            if inspect.isawaitable(result_or_awaitable):
                result = await result_or_awaitable
            else:
                result = result_or_awaitable
            if reporter is not None:
                summary = {
                    "status": result.status.value,
                    "endpoints": {key: value.value for key, value in result.endpoints.items()},
                }
                await reporter.emit(
                    "fetch.run.completed",
                    stage="fetching",
                    data=summary,
                )
                await reporter.complete(
                    _run_status_from_task_status(result.status),
                    summary=summary,
                )
            return result
        except Exception as exc:
            if reporter is not None:
                await reporter.emit(
                    "fetch.run.failed",
                    stage="fetching",
                    level="ERROR",
                    data={"error_type": type(exc).__name__, "error": str(exc)},
                )
                await reporter.complete(
                    "FAILED",
                    summary={
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise

    async def _bind_uid(self, uid: int) -> None:
        """Hook for multi-uid proxy stores; the real FetchingStore ignores it."""
        bind = getattr(self._store, "_bind_uid", None)
        if bind is not None:
            res = bind(uid)
            if inspect.isawaitable(res):
                await res

    def _result_from_scope_decision(self, decision) -> TaskResult | Awaitable[TaskResult]:
        if decision.result is not None:
            return decision.result
        assert decision.scope is not None
        return self._run(decision.scope)

    @staticmethod
    def _derive_status_from_statuses(
        statuses: list[EndpointStatus],
    ) -> TaskStatus:
        if not statuses:
            return TaskStatus.PENDING
        if all(s in (EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM) for s in statuses):
            return TaskStatus.SUCCESS
        has_perm_fail = any(s == EndpointStatus.FAILED_PERMANENT for s in statuses)
        has_exhausted = any(s == EndpointStatus.FAILED_EXHAUSTED for s in statuses)
        has_retryable = any(s == EndpointStatus.FAILED_RETRYABLE for s in statuses)
        has_success = any(s in (EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM) for s in statuses)

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
        """Compatibility shim 鈥?derive task status from a legacy TaskValue.

        New code uses :meth:`_derive_status_from_statuses` directly.
        """
        statuses = [v.status for v in tv.endpoints.values()]
        return Runner._derive_status_from_statuses(statuses)


def _run_status_from_task_status(status: TaskStatus) -> RunStatus:
    if status == TaskStatus.SUCCESS:
        return "SUCCESS"
    if status == TaskStatus.PARTIAL:
        return "PARTIAL"
    if status == TaskStatus.RUNNING:
        return "RUNNING"
    if status == TaskStatus.PENDING:
        return "PENDING"
    return "FAILED"
