# runner._item_fanout — item-level fan-out endpoint execution logic.

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..._logging import Progress
from ..._retry import (
    RetryClassification,
    RetryDriver,
    RetryOutcome,
    RetryPolicy,
)
from .. import (
    AuthError,
    EndpointStatus,
    FetchingError,
    Http412Error,
    ResourceUnavailableError,
)
from ..client import EndpointSpec
from ..env import get_settings
from ..keys import _fetch_key, _item_fetch_key, _progress_key, _task_key

if TYPE_CHECKING:
    pass

logger = logging.getLogger("bili.fetching.runner")


class _ItemFanoutResult(StrEnum):
    """Outcome of _process_single_item for a single item."""
    SUCCESS = "success"
    FAILED = "failed"
    PERMANENT = "permanent"  # AuthError — aborts whole fan-out


def _classify_item_exc(exc: Exception) -> RetryClassification:
    """Classify an item-level fetch exception.

    AuthError and ResourceUnavailableError both terminate the retry loop
    immediately — they signal "no point retrying".  The caller examines the
    re-raised exception type to decide what to return:
      AuthError                    → 'permanent' (caller aborts whole fan-out)
      ResourceUnavailableError     → 'failed'    (only this item, siblings continue)
      other FetchingError exhausted → 'failed'
    Unknown exception types are also permanent: a logic bug shouldn't burn
    the retry budget.
    """
    if isinstance(exc, (AuthError, ResourceUnavailableError)):
        return RetryClassification.PERMANENT
    if isinstance(exc, FetchingError):
        return RetryClassification.RETRYABLE
    return RetryClassification.PERMANENT


class _ItemFanoutMixin:
    """Mixin providing ``_run_item_endpoint`` / ``_process_single_item`` for :class:`Runner`."""

    _data: Any
    _error: Any
    _rl: Any

    async def _update_endpoint_status(self, uid, ep_name, status, **kw) -> None: ...  # pragma: no cover

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
        await self._update_endpoint_status(uid, ep_name, EndpointStatus.RUNNING, retry_count=0)

        # 1. Load source endpoint data
        source_key = _fetch_key(uid, spec.source_endpoint)
        source_data = await self._data.get(source_key)
        if source_data is None or not source_data.get("raw_payload"):
            await self._update_endpoint_status(
                uid, ep_name, EndpointStatus.FAILED_PERMANENT,
            )
            await self._error.record(
                FetchingError(f"source {spec.source_endpoint} data not available"),
                uid=uid, endpoint=ep_name, retryable=False,
            )
            now_ms = int(time.time() * 1000)
            await self._data.put(_fetch_key(uid, ep_name), {
                "uid": uid,
                "endpoint": ep_name,
                "status": EndpointStatus.FAILED_PERMANENT.value,
                "raw_payload": None,
                "fetched_at": now_ms,
                "updated_at": now_ms,
            })
            return

        # 2. Extract items
        items = spec.extract_items(source_data["raw_payload"])
        if not items:
            logger.info(
                "item_endpoint_no_items",
                extra={"uid": uid, "endpoint": ep_name},
            )
            await self._update_endpoint_status(
                uid, ep_name, EndpointStatus.SUCCESS,
            )
            now_ms = int(time.time() * 1000)
            await self._data.put(_fetch_key(uid, ep_name), {
                "uid": uid,
                "endpoint": ep_name,
                "status": EndpointStatus.SUCCESS.value,
                "raw_payload": None,
                "fetched_at": now_ms,
                "updated_at": now_ms,
                "item_counts": {"total": 0, "completed": 0, "failed": 0},
            })
            return

        total_items = len(items)

        # Shared counters — safe in single-threaded asyncio as long as
        # increments happen outside any ``await`` expression.
        completed_items = 0
        failed_items = 0

        settings = get_settings()
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
            now_ms = int(time.time() * 1000)
            progress_val = {
                "mode": "item_fanout",
                "total_items": total_items,
                "completed_items": completed_items,
                "failed_items": failed_items,
                "done": False,
                "updated_at": now_ms,
            }
            await self._data.put(_progress_key(uid, ep_name), progress_val)
            await self._data.update_task_endpoint(
                _task_key(uid), ep_name, EndpointStatus.RUNNING.value,
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
                await self._update_endpoint_status(
                    uid, ep_name, EndpointStatus.FAILED_PERMANENT,
                )
                now_ms = int(time.time() * 1000)
                await self._data.put(_fetch_key(uid, ep_name), {
                    "uid": uid,
                    "endpoint": ep_name,
                    "status": EndpointStatus.FAILED_PERMANENT.value,
                    "raw_payload": None,
                    "fetched_at": now_ms,
                    "updated_at": now_ms,
                })
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

        now_ms = int(time.time() * 1000)
        final_progress = {
            "mode": "item_fanout",
            "total_items": total_items,
            "completed_items": completed_items,
            "failed_items": failed_items,
            "done": True,
            "updated_at": now_ms,
        }
        await self._data.put(_progress_key(uid, ep_name), final_progress)

        await self._update_endpoint_status(
            uid, ep_name, final_status,
            item_progress={
                "total": total_items,
                "completed": completed_items,
                "failed": failed_items,
            },
        )

        # Write endpoint-level fetch key so query layer can read status.
        # Individual item payloads live in per-bvid keys; this record only
        # carries the aggregate status and item counts.
        await self._data.put(_fetch_key(uid, ep_name), {
            "uid": uid,
            "endpoint": ep_name,
            "status": final_status.value,
            "raw_payload": None,
            "fetched_at": now_ms,
            "updated_at": now_ms,
            "item_counts": {
                "total": total_items,
                "completed": completed_items,
                "failed": failed_items,
            },
        })

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
        """Fetch and store a single item. Returns an _ItemFanoutResult enum value.

        'permanent' signals an AuthError that should abort the whole fan-out.
        'failed' covers everything else that didn't succeed (including
        ResourceUnavailableError, exhausted retries, and unexpected errors).
        """
        ep_name = spec.name

        # Incremental / refresh: skip if already stored (and fresh for refresh).
        if mode in ("incremental", "refresh"):
            existing = await self._data.get(_item_fetch_key(uid, ep_name, item_id))
            if existing is not None:
                if mode == "incremental":
                    return _ItemFanoutResult.SUCCESS
                # refresh mode: check freshness window
                fetched_at = existing.get("fetched_at")
                if fetched_at is not None:
                    now_ms = int(time.time() * 1000)
                    age_ms = now_ms - fetched_at
                    threshold_ms = get_settings().bili_fetching_refresh_after_days * 86400 * 1000
                    if age_ms < threshold_ms:
                        return _ItemFanoutResult.SUCCESS  # still fresh, skip

        settings = get_settings()
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_retry_delays()
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
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable=False,
                )
                return None

            if isinstance(exc, ResourceUnavailableError):
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable=False,
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
                await self._data.put("rate_limit:global", self._rl.to_state())
                await self._data.put(
                    f"rate_limit:{spec.rate_limit_key}",
                    self._rl.to_state(endpoint=spec.rate_limit_key),
                )
                retry_state["count"] += 1
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
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
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
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

            # Unknown error → wrap and record as failed.
            wrapped = FetchingError(f"unexpected: {type(exc).__name__}: {exc}")
            await self._error.record(
                wrapped, uid=uid, endpoint=ep_name,
                retryable=False,
                detail={"item_id": item_id},
            )
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=_classify_item_exc,
        )
        driver = RetryDriver(policy)

        try:
            result = await driver.run(
                _do_fetch, on_attempt_failed=_on_attempt_failed,
            )
        except AuthError:
            return _ItemFanoutResult.PERMANENT
        except Exception:
            # ResourceUnavailableError / exhausted FetchingError / unknown:
            # all map to a single-item failure that lets siblings continue.
            return _ItemFanoutResult.FAILED

        # Success — store item.
        now_ms = int(time.time() * 1000)
        fetch_val = {
            "uid": uid,
            "endpoint": ep_name,
            "item_id": item_id,
            "status": "SUCCESS",
            "raw_payload": result,
            "fetched_at": now_ms,
            "updated_at": now_ms,
        }
        await self._data.put(
            _item_fetch_key(uid, ep_name, item_id), fetch_val,
        )
        logger.info(
            "item_endpoint_item_saved",
            extra={"uid": uid, "endpoint": ep_name, "item_id": item_id},
        )
        return _ItemFanoutResult.SUCCESS
