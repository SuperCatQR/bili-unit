# runner._item_fanout — item-level fan-out endpoint execution logic.

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..._logging import Progress
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
                uid=uid, endpoint=ep_name, retryable="false",
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
            if result == "success":
                completed_items += 1
            elif result == "permanent":
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
    ) -> str:
        """Fetch and store a single item. Returns 'success' | 'failed' | 'permanent'.

        'permanent' signals an AuthError that should abort the whole fan-out.
        """
        from . import _get_retry_delays

        ep_name = spec.name

        # Incremental / refresh: skip if already stored (and fresh for refresh).
        if mode in ("incremental", "refresh"):
            existing = await self._data.get(_item_fetch_key(uid, ep_name, item_id))
            if existing is not None:
                if mode == "incremental":
                    return "success"
                # refresh mode: check freshness window
                fetched_at = existing.get("fetched_at")
                if fetched_at is not None:
                    now_ms = int(time.time() * 1000)
                    age_ms = now_ms - fetched_at
                    threshold_ms = get_settings().bili_fetching_refresh_after_days * 86400 * 1000
                    if age_ms < threshold_ms:
                        return "success"  # still fresh, skip

        settings = get_settings()
        max_retries = settings.bili_fetching_max_retries
        RETRY_DELAYS = _get_retry_delays()
        item_retry = 0

        while True:
            try:
                await self._rl.acquire(spec.rate_limit_key)
                result = await spec.callable(
                    item_id, credential, _uid=uid,
                    timeout=settings.bili_fetching_request_timeout,
                )
            except AuthError as exc:
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable="false",
                )
                return "permanent"
            except ResourceUnavailableError as exc:
                # Permanent per-item failure — skip retries.  Unlike AuthError,
                # this only fails the single item; sibling items continue.
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable="false",
                    detail={"item_id": item_id},
                )
                logger.info(
                    "item_endpoint_item_unavailable",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "reason": str(exc),
                    },
                )
                return "failed"
            except Http412Error as exc:
                advice = await self._rl.record_412(spec.rate_limit_key)
                await self._data.put("rate_limit:global", self._rl.to_state())
                await self._data.put(
                    f"rate_limit:{spec.rate_limit_key}",
                    self._rl.to_state(endpoint=spec.rate_limit_key),
                )
                item_retry += 1
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
                    retryable="true",
                    detail={"item_id": item_id, "retry_count": item_retry},
                )
                if item_retry >= max_retries:
                    logger.warning(
                        "item_endpoint_item_exhausted",
                        extra={
                            "uid": uid, "endpoint": ep_name,
                            "item_id": item_id, "retry": item_retry,
                        },
                    )
                    return "failed"
                wait = max(
                    advice.get("wait_seconds", 0),
                    RETRY_DELAYS[min(item_retry - 1, len(RETRY_DELAYS) - 1)],
                )
                logger.info(
                    "item_endpoint_retry",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "wait_s": wait,
                        "retry": item_retry,
                    },
                )
                await asyncio.sleep(wait)
                continue
            except FetchingError as exc:
                item_retry += 1
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
                    retryable="true" if item_retry < max_retries else "false",
                    detail={"item_id": item_id},
                )
                if item_retry >= max_retries:
                    logger.warning(
                        "item_endpoint_item_exhausted",
                        extra={
                            "uid": uid, "endpoint": ep_name,
                            "item_id": item_id, "retry": item_retry,
                            "reason": str(exc),
                        },
                    )
                    return "failed"
                wait = RETRY_DELAYS[min(item_retry - 1, len(RETRY_DELAYS) - 1)]
                logger.info(
                    "item_endpoint_retry",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "item_id": item_id, "wait_s": wait,
                        "retry": item_retry, "reason": str(exc),
                    },
                )
                await asyncio.sleep(wait)
                continue
            except Exception as exc:
                from .. import FetchingError as _FE
                wrapped = _FE(f"unexpected: {type(exc).__name__}: {exc}")
                await self._error.record(
                    wrapped, uid=uid, endpoint=ep_name,
                    retryable="false",
                    detail={"item_id": item_id},
                )
                return "failed"

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
            return "success"
