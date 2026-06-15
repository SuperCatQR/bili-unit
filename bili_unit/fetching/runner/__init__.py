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
    FetchingError,
    TaskResult,
    TaskStatus,
)
from .._bilibili_adapter import (
    fetch_endpoint as _real_fetch_endpoint,
)
from .._endpoint_catalog import ENDPOINTS, get_endpoint
from .._endpoint_spec import EndpointSpec
from ..auth import get_credential
from ..data import DataStore
from ..error import ErrorStore
from ..keys import _progress_key, _task_key
from ..rate_limit import RateLimitController
from ..task import EndpointEntry, TaskValue
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
        self._stale_threshold_ms = stale_running_threshold_ms
        self._fetch_fn = fetch_fn if fetch_fn is not None else _real_fetch_endpoint

    # -- public ------------------------------------------------------------

    async def run_task(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental"
    ) -> TaskResult:
        ep_names = endpoints or [ep.name for ep in ENDPOINTS]
        return await self._run(uid, ep_names, fresh=True, mode=mode)

    async def resume_task(self, uid: int, endpoints: list[str] | None = None) -> TaskResult:
        tv = await self._load_task(uid)
        if tv is None:
            return TaskResult(uid=uid, status=TaskStatus.PENDING)
        ep_names = list(tv.endpoints.keys())
        # Merge new endpoints into existing task
        if endpoints is not None:
            now_ms = int(time.time() * 1000)
            for ep in endpoints:
                if ep not in tv.endpoints:
                    tv.endpoints[ep] = EndpointEntry(status=EndpointStatus.PENDING)
                    ep_names.append(ep)
            tv.updated_at = now_ms
            await self._save_task(tv)
        if not ep_names:
            return TaskResult(uid=uid, status=TaskStatus.PENDING)
        return await self._run(uid, ep_names, fresh=False)

    async def run_or_resume(
        self, uid: int, endpoints: list[str] | None = None, mode: str = "incremental"
    ) -> TaskResult:
        """Entry point for command: new task or resume existing.

        Handles idempotency internally — command need not check task state.

        Mode behaviour:
          incremental — SUCCESS task enters incremental scan (not skipped).
          full        — SUCCESS task triggers full re-fetch.
        """
        tv = await self._load_task(uid)
        if tv is None:
            return await self.run_task(uid, endpoints, mode=mode)

        if tv.status == TaskStatus.RUNNING:
            now_ms = int(time.time() * 1000)
            age_ms = now_ms - (tv.updated_at or 0)
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
            tv.status = TaskStatus.PARTIAL
            tv.updated_at = now_ms
            await self._save_task(tv)

        if tv.status == TaskStatus.FAILED_PERMANENT:
            logger.info("task_failed_permanent_skip", extra={"uid": uid})
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)

        if tv.status == TaskStatus.SUCCESS:
            if mode in ("incremental", "refresh"):
                logger.info("task_incremental_scan", extra={"uid": uid, "mode": mode})
                return await self._run(uid, endpoints or [ep.name for ep in ENDPOINTS], fresh=False, mode=mode)
            elif mode == "full":
                logger.info("task_full_refetch", extra={"uid": uid})
                return await self.run_task(uid, endpoints, mode=mode)
            # fallback: skip
            return TaskResult(uid=uid, status=TaskStatus.SUCCESS)

        # PARTIAL / FAILED_RETRYABLE / FAILED_EXHAUSTED → resume
        if mode == "full":
            # full mode resets all endpoints
            return await self.run_task(uid, endpoints, mode=mode)
        return await self.resume_task(uid, endpoints)

    # -- internal hub ------------------------------------------------------

    async def _run(
        self, uid: int, endpoints: list[str], fresh: bool, mode: str = "incremental"
    ) -> TaskResult:
        # auth — always required; fetching does not operate without credential
        try:
            credential = await get_credential()
        except AuthError as exc:
            await self._error.record(exc, uid=uid, retryable=False)
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)

        # load / create task
        if fresh:
            tv = TaskValue(uid=uid, status=TaskStatus.RUNNING)
            now_ms = int(time.time() * 1000)
            tv.created_at = now_ms
            tv.updated_at = now_ms
            for ep in endpoints:
                tv.endpoints[ep] = EndpointEntry(status=EndpointStatus.PENDING)
        else:
            tv = await self._load_task(uid)
            if tv is None:
                return TaskResult(uid=uid, status=TaskStatus.PENDING)
            tv.status = TaskStatus.RUNNING
            tv.updated_at = int(time.time() * 1000)
            for ep_name in endpoints:
                entry = tv.endpoints.get(ep_name)
                if entry is None:
                    continue
                spec = get_endpoint(ep_name)
                if entry.status == EndpointStatus.FAILED_EXHAUSTED:
                    entry.retry_count = 0
                    entry.status = EndpointStatus.PENDING
                elif entry.status == EndpointStatus.SUCCESS and mode in ("incremental", "refresh"):
                    # Reset endpoints in the current run list for re-scan.
                    # Only endpoints explicitly in `endpoints` are iterated here.
                    entry.status = EndpointStatus.PENDING

        await self._save_task(tv)

        # ---- Phase 1: uid-level endpoints (parallel) ----
        uid_tasks = []
        item_specs: list[tuple[str, EndpointSpec]] = []

        for ep_name in endpoints:
            entry = tv.endpoints.get(ep_name)
            if entry is None or entry.status == EndpointStatus.SUCCESS:
                continue
            spec = get_endpoint(ep_name)
            if spec is None:
                entry.status = EndpointStatus.FAILED_PERMANENT
                await self._error.record(
                    FetchingError(f"unknown endpoint: {ep_name}"),
                    uid=uid, endpoint=ep_name, retryable=False,
                )
                continue

            if spec.kind == "uid":
                uid_tasks.append(
                    self._run_endpoint(uid, spec, ep_name, credential, mode)
                )
            elif spec.kind == "item":
                item_specs.append((ep_name, spec))
                # ensure source_endpoint is in the task
                if spec.source_endpoint:
                    src_entry = tv.endpoints.get(spec.source_endpoint)
                    if src_entry is None:
                        # source not in task → add and run in Phase 1
                        tv.endpoints[spec.source_endpoint] = EndpointEntry(
                            status=EndpointStatus.PENDING,
                        )
                        src_spec = get_endpoint(spec.source_endpoint)
                        if src_spec is not None:
                            uid_tasks.append(
                                self._run_endpoint(
                                    uid, src_spec, spec.source_endpoint,
                                    credential, mode,
                                )
                            )
                        await self._save_task(tv)
                    # If source already SUCCESS (data exists), don't re-add
                    # Phase 2 will check source status after Phase 1 completes

        if uid_tasks:
            with Progress(
                total=len(uid_tasks),
                label=f"fetch uid={uid} endpoints",
            ) as bar:
                await self._gather_with_progress(uid_tasks, bar)

        # ---- Phase 2: item-level fan-out endpoints ----
        if item_specs:
            # reload task to get Phase 1 results
            tv = await self._load_task(uid)
            if tv is None:
                return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)

            item_tasks = []
            for ep_name, spec in item_specs:
                entry = tv.endpoints.get(ep_name)
                if entry is None or entry.status == EndpointStatus.SUCCESS:
                    continue
                # check source endpoint status
                if spec.source_endpoint:
                    src_entry = tv.endpoints.get(spec.source_endpoint)
                    if src_entry is None or src_entry.status != EndpointStatus.SUCCESS:
                        entry.status = EndpointStatus.FAILED_PERMANENT
                        await self._error.record(
                            FetchingError(
                                f"source endpoint {spec.source_endpoint} not SUCCESS"
                            ),
                            uid=uid, endpoint=ep_name, retryable=False,
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
                    self._run_item_endpoint(uid, spec, credential, mode)
                )

            if item_tasks:
                with Progress(
                    total=len(item_tasks),
                    label=f"fetch uid={uid} items",
                ) as bar:
                    await self._gather_with_progress(item_tasks, bar)

        # final summary
        tv = await self._load_task(uid)
        if tv is None:
            return TaskResult(uid=uid, status=TaskStatus.FAILED_PERMANENT)
        tv.status = self._derive_status(tv)
        tv.updated_at = int(time.time() * 1000)
        tv.failed_item_ids = await self._collect_failed_item_ids(uid, tv)
        await self._save_task(tv)
        return TaskResult(
            uid=uid, status=tv.status,
            endpoints={k: v.status for k, v in tv.endpoints.items()},
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    async def _gather_with_progress(coros: list, bar: Progress) -> None:
        """gather() variant that ticks ``bar`` once per coro completion.

        Wraps each coroutine in a small shim so we don't depend on
        ``asyncio.as_completed`` (which loses task identity on cancel).
        Exceptions are swallowed at this layer — endpoints already record
        their own errors via ``self._error.record``; this matches the prior
        ``return_exceptions=True`` behaviour.
        """
        async def _wrap(coro):
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 — preserve gather semantics
                return exc
            finally:
                bar.update(1)

        await asyncio.gather(*[_wrap(c) for c in coros])

    async def _load_task(self, uid: int) -> TaskValue | None:
        d = await self._data.get(_task_key(uid))
        if d is None:
            return None
        return TaskValue.from_dict(d)

    async def _load_progress(self, uid: int, endpoint: str) -> dict | None:
        return await self._data.get(_progress_key(uid, endpoint))

    async def _save_task(self, tv: TaskValue) -> None:
        await self._data.put(_task_key(tv.uid), tv.to_dict())

    async def _update_endpoint_status(
        self, uid: int, ep_name: str, status: EndpointStatus,
        retry_count: int = 0, last_error_id: int | None = None,
        item_progress: dict | None = None,
    ) -> None:
        await self._data.update_task_endpoint(
            _task_key(uid), ep_name, status.value,
            retry_count=retry_count, last_error_id=last_error_id,
            item_progress=item_progress,
        )

    @staticmethod
    def _derive_status(tv: TaskValue) -> TaskStatus:
        statuses = [v.status for v in tv.endpoints.values()]
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

    async def _collect_failed_item_ids(
        self, uid: int, tv: TaskValue,
    ) -> list[str]:
        """Aggregate ``failed_item_ids`` from the error store and endpoint entries.

        Encoding:
          * uid-level endpoint failure → ``"endpoint"``.
          * item-level fan-out failure → ``"endpoint:item_id"``.

        For item-level entries an error record means "this item failed at
        least once"; the *current* truth is whether a SUCCESS record now
        exists in the data store (item-level fan-out only writes a
        per-item record on success). So we cross-check each errored
        item against ``uid:{uid}:fetch:{ep}:{item_id}`` and drop items
        that have since succeeded — without this filter, a retry-to-success
        leaves the previous error record causing stale ``failed_item_ids``.

        Order is deterministic (stable sort on the joined string) so callers
        get a predictable diff.
        """
        ids: set[str] = set()
        try:
            errors = await self._error.list_by_uid(uid)
        except Exception:  # noqa: BLE001 — never fail task save on error-log read
            errors = []

        item_level_eps = {
            name for name, entry in tv.endpoints.items()
            if entry.item_progress is not None
        }

        # Pre-load successful item keys once per item-level endpoint to
        # avoid one data store get per error record.
        succeeded_items: set[tuple[str, str]] = set()
        for ep in item_level_eps:
            try:
                rows = await self._data.list_prefix(f"uid:{uid}:fetch:{ep}:")
            except Exception:  # noqa: BLE001
                continue
            for _, v in rows:
                if isinstance(v, dict) and v.get("status") == "SUCCESS":
                    iid = v.get("item_id")
                    if iid:
                        succeeded_items.add((ep, str(iid)))

        for err in errors:
            ep = err.endpoint
            if not ep:
                continue
            detail = err.detail or {}
            item_id = detail.get("item_id") if isinstance(detail, dict) else None
            if item_id:
                # If this item later succeeded, skip — the SUCCESS record
                # supersedes the historical error.
                if (ep, str(item_id)) in succeeded_items:
                    continue
                ids.add(f"{ep}:{item_id}")
            elif ep in item_level_eps:
                # Item-level endpoint with no item_id detail — treat as
                # endpoint-level (e.g. fan-out source dependency failed).
                ids.add(ep)
            else:
                ids.add(ep)

        for name, entry in tv.endpoints.items():
            if entry.last_error_id is None:
                continue
            if entry.status in (
                EndpointStatus.SUCCESS,
                EndpointStatus.PARTIAL_ITEM,
            ):
                continue
            if name not in item_level_eps:
                ids.add(name)

        return sorted(ids)
