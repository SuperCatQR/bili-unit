# runner._item_fanout — item-level fan-out endpoint execution logic.

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..._logging import Progress
from ..._retry import RetryDriver, RetryOutcome, RetryPolicy
from .. import (
    AuthError,
    EndpointStatus,
    FetchingError,
    Http412Error,
    ResourceUnavailableError,
)
from .._endpoint_spec import EndpointSpec
from ._failure import classify_fetching_exception

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from .._store import FetchingStore

logger = logging.getLogger("bili.fetching.runner")


class _ItemFanoutResult(StrEnum):
    """Outcome of _process_single_item for a single item."""
    SUCCESS = "success"
    FAILED = "failed"
    PERMANENT = "permanent"  # AuthError — aborts whole fan-out


class _ItemFanoutMixin:
    """Mixin providing ``_run_item_endpoint`` / ``_process_single_item`` for :class:`Runner`."""

    _store: FetchingStore
    _rl: Any
    _settings: BiliSettings

    # -- item-level fan-out endpoint ------------------------------------

    async def _run_item_endpoint(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        credential: Any,
        mode: str = "incremental",
    ) -> None:
        """Execute an item-level fan-out endpoint (e.g. video_detail).

        Reads items from the source endpoint's stored data, fetches each item
        individually (up to ``item_concurrency`` in parallel), and stores
        results per item_id.
        """
        ep_name = spec.name
        await self._store.update_endpoint_state(
            ep_name,
            status=EndpointStatus.RUNNING.value,
            retry_count=0,
        )

        # 1. Load source endpoint data
        source_payload = (
            await self._store.get_raw_payload(spec.source_endpoint)
            if spec.source_endpoint
            else None
        )
        if not source_payload:
            err_id = await self._store.record_error(
                endpoint=ep_name,
                error_type="FetchingError",
                message=f"source {spec.source_endpoint} data not available",
                retryable=False,
            )
            await self._store.update_endpoint_state(
                ep_name,
                status=EndpointStatus.FAILED_PERMANENT.value,
                last_error_id=err_id,
            )
            return

        # 2. Extract items
        items = spec.extract_items(source_payload)
        if not items:
            logger.info(
                "item_endpoint_no_items",
                extra={"uid": uid, "endpoint": ep_name},
            )
            await self._store.update_endpoint_state(
                ep_name,
                status=EndpointStatus.SUCCESS.value,
                item_progress={"total": 0, "completed": 0, "failed": 0},
            )
            await self._store.save_progress(
                ep_name,
                {"cursor": None, "total": 0, "fetched": 0},
            )
            return

        total_items = len(items)
        completed_items = 0
        failed_items = 0

        settings = self._settings
        max_concurrent = max(settings.bili_fetching_item_concurrency, 1)
        semaphore = asyncio.Semaphore(max_concurrent)

        bar = Progress(total=total_items, label=f"fetch uid={uid} {ep_name}")

        async def process_item(item_id: str) -> str:
            nonlocal completed_items, failed_items
            async with semaphore:
                result = await self._process_single_item(
                    uid, spec, credential, item_id, mode,
                )
            if result == _ItemFanoutResult.SUCCESS:
                completed_items += 1
            elif result == _ItemFanoutResult.PERMANENT:
                # AuthError — abort the whole fan-out immediately.
                raise FetchingError("auth_permanent_fail")
            else:
                failed_items += 1

            bar.update(1, postfix=f"ok={completed_items} fail={failed_items}")

            # Update live progress (non-atomic; fine in single-threaded asyncio).
            await self._store.save_progress(
                ep_name,
                {
                    "cursor": "running",
                    "total": total_items,
                    "fetched": completed_items,
                },
            )
            await self._store.update_endpoint_state(
                ep_name,
                status=EndpointStatus.RUNNING.value,
                item_progress={
                    "total": total_items,
                    "completed": completed_items,
                    "failed": failed_items,
                },
            )
            return result

        # 3. Run all items concurrently (bounded by semaphore).
        try:
            try:
                await asyncio.gather(
                    *[process_item(iid) for iid in items],
                    return_exceptions=False,
                )
            except FetchingError:
                # AuthError from any item → abort and mark permanently failed.
                await self._store.update_endpoint_state(
                    ep_name,
                    status=EndpointStatus.FAILED_PERMANENT.value,
                )
                return
        finally:
            bar.close()

        # 4. Final status
        if failed_items > 0 and completed_items > 0:
            final_status = EndpointStatus.PARTIAL_ITEM
        elif failed_items > 0 and completed_items == 0:
            final_status = EndpointStatus.FAILED_EXHAUSTED
        else:
            final_status = EndpointStatus.SUCCESS

        await self._store.save_progress(
            ep_name,
            {
                "cursor": None,
                "total": total_items,
                "fetched": completed_items,
            },
        )

        await self._store.update_endpoint_state(
            ep_name,
            status=final_status.value,
            item_progress={
                "total": total_items,
                "completed": completed_items,
                "failed": failed_items,
            },
        )

        logger.info(
            "item_endpoint_completed",
            extra={
                "uid": uid, "endpoint": ep_name,
                "status": final_status.value,
                "completed": completed_items,
                "failed": failed_items,
                "total": total_items,
            },
        )

    async def _process_single_item(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        credential: Any,
        item_id: str,
        mode: str,
    ) -> _ItemFanoutResult:
        """Fetch and store a single item. Returns an _ItemFanoutResult enum value."""
        ep_name = spec.name

        # Incremental / refresh: skip if already stored (and fresh for refresh).
        if mode in ("incremental", "refresh"):
            existing = await self._store.get_raw_payload(ep_name, item_id)
            if existing is not None:
                if mode == "incremental":
                    return _ItemFanoutResult.SUCCESS
                # refresh mode: check freshness window
                ages = await self._store.list_item_ages_ms(ep_name)
                fetched_at = ages.get(item_id)
                if fetched_at is not None:
                    now_ms = int(time.time() * 1000)
                    age_ms = now_ms - fetched_at
                    threshold_ms = self._settings.bili_fetching_refresh_after_days * 86400 * 1000
                    if age_ms < threshold_ms:
                        return _ItemFanoutResult.SUCCESS  # still fresh, skip

        settings = self._settings
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_fetching_retry_delays()
        retry_state = {"count": 0}

        async def _do_fetch():
            await self._rl.acquire(spec.rate_limit_key)
            extra_kw: dict = {"timeout": settings.bili_fetching_request_timeout}
            if spec.needs_parent_uid:
                extra_kw["_uid"] = uid
            return await spec.callable(item_id, credential, **extra_kw)

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            if isinstance(exc, AuthError):
                await self._store.record_error(
                    endpoint=ep_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    retryable=False,
                )
                return None

            if isinstance(exc, ResourceUnavailableError):
                await self._store.record_error(
                    endpoint=ep_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    retryable=False,
                    detail={"item_id": item_id},
                )
                logger.info(
                    "item_endpoint_item_unavailable",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "reason": str(exc),
                    },
                )
                return None

            if isinstance(exc, Http412Error):
                advice = await self._rl.record_412(spec.rate_limit_key)
                # rate-limit state is in-memory only; no persistence call here.
                retry_state["count"] += 1
                await self._store.record_error(
                    endpoint=ep_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    retryable=outcome.will_retry,
                    detail={"item_id": item_id, "retry_count": retry_state["count"]},
                )
                if not outcome.will_retry:
                    logger.warning(
                        "item_endpoint_item_exhausted",
                        extra={
                            "uid": uid, "endpoint": ep_name,
                            "item_id": item_id, "retry": retry_state["count"],
                        },
                    )
                    return None
                wait = max(advice.get("wait_seconds", 0), outcome.delay_seconds)
                logger.info(
                    "item_endpoint_retry",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "wait_s": wait,
                        "retry": retry_state["count"],
                    },
                )
                return wait

            if isinstance(exc, FetchingError):
                retry_state["count"] += 1
                await self._store.record_error(
                    endpoint=ep_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    retryable=outcome.will_retry,
                    detail={"item_id": item_id},
                )
                if not outcome.will_retry:
                    logger.warning(
                        "item_endpoint_item_exhausted",
                        extra={
                            "uid": uid, "endpoint": ep_name,
                            "item_id": item_id, "retry": retry_state["count"],
                            "reason": str(exc),
                        },
                    )
                    return None
                logger.info(
                    "item_endpoint_retry",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "wait_s": outcome.delay_seconds,
                        "retry": retry_state["count"], "reason": str(exc),
                    },
                )
                return None

            await self._store.record_error(
                endpoint=ep_name,
                error_type="FetchingError",
                message=f"unexpected: {type(exc).__name__}: {exc}",
                retryable=False,
                detail={"item_id": item_id},
            )
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=classify_fetching_exception,
        )
        driver = RetryDriver(policy)

        try:
            result = await driver.run(
                _do_fetch, on_attempt_failed=_on_attempt_failed,
            )
        except AuthError:
            return _ItemFanoutResult.PERMANENT
        except Exception:
            return _ItemFanoutResult.FAILED

        # Success — store item.
        await self._store.save_raw_payload(ep_name, item_id, result)
        logger.info(
            "item_endpoint_item_saved",
            extra={"uid": uid, "endpoint": ep_name, "item_id": item_id},
        )
        return _ItemFanoutResult.SUCCESS
