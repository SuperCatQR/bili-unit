# runner._transform — transform pipeline (scan → enqueue → workers → rollup).

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..._logging import Progress
from ..._retry import (
    RetryClassification,
    RetryDriver,
    RetryOutcome,
    RetryPolicy,
)
from .. import (
    ProcessingError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
)
from ..keys import _proc_key, _task_key
from ..task import PipelineEntry, ProcessingTaskValue
from ..transform import get_handler
from ..transform._base import WorkItem

if TYPE_CHECKING:
    from ..env import ProcessingEnv

logger = logging.getLogger("bili.processing.runner")


_TRANSFORM = "transform"
_FANOUT_PAYLOAD_ENDPOINTS = frozenset({
    "article_detail",
    "opus_detail",
    "article_list_detail",
})


class _TransformMixin:
    """Mixin providing transform pipeline methods for :class:`ProcessingRunner`.

    Accesses runner state (``self._data``, ``self._error``, ``self._fetch_qry``,
    ``self._settings``) and helpers (``_write_progress``, ``_derive_pipeline_status``,
    ``_is_retryable``) via the combined MRO at runtime.
    """

    _data: Any
    _error: Any
    _fetch_qry: Any
    _settings: ProcessingEnv

    async def _write_progress(
        self, uid: int, pipeline: str, item_type: str,
        counts: dict[str, int], done: bool,
    ) -> None: ...  # pragma: no cover

    @staticmethod
    def _derive_pipeline_status(
        rollup: dict[str, dict[str, int]],
    ) -> ProcessingPipelineStatus: ...  # pragma: no cover

    @staticmethod
    def _is_retryable(exc: Exception) -> bool: ...  # pragma: no cover

    # -- transform pipeline ------------------------------------------------

    async def _run_transform(
        self: Any,
        uid: int,
        tv: ProcessingTaskValue,
        item_types: list[str],
        mode: str,
    ) -> None:
        """Phase-1 transform: scan → enqueue → workers → rollup."""
        entry = tv.pipelines.setdefault(_TRANSFORM, PipelineEntry())
        entry.status = ProcessingPipelineStatus.RUNNING
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=entry.items,
        )

        # 1. discover work items per item_type
        rollup: dict[str, dict[str, int]] = {}
        all_items: list[WorkItem] = []
        for it in item_types:
            handler = get_handler(it)
            if handler is None:
                continue
            try:
                discovered = await self._discover_items(uid, handler, mode)
            except ProcessingError as exc:
                logger.warning(
                    "transform_discovery_failed",
                    extra={"uid": uid, "item_type": it, "error": str(exc)},
                )
                rollup[it] = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
                continue

            ready, skipped = discovered
            rollup[it] = {
                "total": len(ready) + skipped,
                "completed": 0,
                "failed": 0,
                "skipped": skipped,
            }
            all_items.extend(ready)

        entry.items = rollup
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=entry.items,
        )

        # 2. write progress markers (initial)
        for it, counts in rollup.items():
            await self._write_progress(uid, _TRANSFORM, it, counts, done=False)

        # 3. queue + worker pool
        if all_items:
            await self._dispatch_workers(uid, all_items, rollup)

        # 4. final pipeline status
        entry.status = self._derive_pipeline_status(rollup)
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=rollup,
        )
        for it, counts in rollup.items():
            await self._write_progress(uid, _TRANSFORM, it, counts, done=True)

    async def _discover_items(
        self: Any,
        uid: int,
        handler: Any,
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Phase-0 discovery for a single transform handler.

        Returns (ready_items, skipped_count). 'skipped' counts items already
        SUCCESS in incremental mode.

        Per processing design §10.1 fetching consumption rule: only emit
        items when the source endpoint is SUCCESS for uid-level, or
        PARTIAL_ITEM/SUCCESS for item-level fan-out (only the SUCCESS items
        are enumerated).
        """
        optional_eps: tuple[str, ...] = tuple(
            getattr(handler, "optional_endpoints", ()) or (),
        )
        raw_payloads: dict[str, dict] = {}
        for ep in handler.source_endpoints:
            if ep == "video_detail":
                # video_detail is the only fan-out source that emits one
                # WorkItem per successful item payload instead of passing the
                # whole {item_id -> payload} map into a parent-list handler.
                detail_payloads = await self._fetch_qry.list_fanout_payloads(uid, ep)
                if not detail_payloads:
                    logger.info(
                        "transform_endpoint_unavailable",
                        extra={"uid": uid, "item_type": handler.item_type,
                               "endpoint": ep},
                    )
                    return [], 0
                items: list[WorkItem] = []
                for raw_payload in detail_payloads.values():
                    items.extend(handler.extract_items({"video_detail": raw_payload}))
                return await self._filter_ready(uid, handler, items, mode)

            if ep in _FANOUT_PAYLOAD_ENDPOINTS:
                # Optional fan-out enrichments are collected as
                # {item_id -> raw_payload}; handlers join this map with the
                # required uid-level listing endpoint.
                raw_payloads[ep] = await self._fetch_qry.list_fanout_payloads(uid, ep)
                continue

            # uid-level endpoint
            ep_dto = await self._fetch_qry.get_endpoint(uid, ep)
            if ep_dto is None or not ep_dto.available:
                if ep in optional_eps:
                    # Optional endpoint missing → omit from raw_payloads but
                    # keep the handler running (handler decides whether to
                    # emit/skip the optional fields).
                    logger.info(
                        "transform_optional_endpoint_unavailable",
                        extra={"uid": uid, "item_type": handler.item_type,
                               "endpoint": ep},
                    )
                    continue
                logger.info(
                    "transform_endpoint_unavailable",
                    extra={"uid": uid, "item_type": handler.item_type, "endpoint": ep},
                )
                return [], 0
            raw_payloads[ep] = ep_dto.raw_payload or {}

        items = handler.extract_items(raw_payloads)
        return await self._filter_ready(uid, handler, items, mode)

    async def _filter_ready(
        self: Any,
        uid: int,
        handler: Any,
        items: list[WorkItem],
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Apply incremental skip rule: SUCCESS already-stored items are skipped."""
        if mode == "full":
            return items, 0
        # incremental: skip items already SUCCESS, retry FAILED, run new
        ready: list[WorkItem] = []
        skipped = 0
        for item in items:
            existing = await self._data.get(_proc_key(uid, item.item_type, item.item_id))
            if existing is None:
                ready.append(item)
                continue
            status = existing.get("status")
            if status == ProcessingItemStatus.SUCCESS.value:
                skipped += 1
                continue
            # FAILED / SKIPPED / PROCESSING / PENDING → retry once
            ready.append(item)
        return ready, skipped

    async def _dispatch_workers(
        self: Any,
        uid: int,
        items: list[WorkItem],
        rollup: dict[str, dict[str, int]],
    ) -> None:
        """Run transform workers over ``items``; updates rollup in-place."""
        worker_count = max(1, int(self._settings.bili_processing_transform_workers))
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, int(self._settings.bili_processing_queue_maxsize)),
        )
        rollup_lock = asyncio.Lock()
        bar = Progress(total=len(items), label=f"transform uid={uid}")

        async def producer() -> None:
            for item in items:
                await queue.put(item)
            for _ in range(worker_count):
                await queue.put(None)  # sentinel

        async def worker(idx: int) -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                handler = get_handler(item.item_type)
                if handler is None:
                    async with rollup_lock:
                        bucket = rollup.setdefault(
                            item.item_type,
                            {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                        )
                        bucket["failed"] += 1
                    bar.update(1, postfix=f"skip {item.item_type}")
                    continue
                ok = await self._process_one(uid, handler, item)
                async with rollup_lock:
                    bucket = rollup.setdefault(
                        item.item_type,
                        {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                    )
                    if ok:
                        bucket["completed"] += 1
                    else:
                        bucket["failed"] += 1
                bar.update(
                    1,
                    postfix=f"{item.item_type}/{item.item_id} {'ok' if ok else 'fail'}",
                )

        try:
            await asyncio.gather(
                producer(),
                *[worker(i) for i in range(worker_count)],
            )
        finally:
            bar.close()

    async def _process_one(
        self: Any,
        uid: int,
        handler: Any,
        item: WorkItem,
    ) -> bool:
        key = _proc_key(uid, item.item_type, item.item_id)
        max_retries = self._settings.bili_processing_max_retries
        retry_delays = self._settings.get_retry_delays()

        retry_state = {"count": 0}

        async def _do_transform():
            return handler.transform(item)

        def _classify(exc: Exception) -> RetryClassification:
            return (
                RetryClassification.RETRYABLE
                if self._is_retryable(exc)
                else RetryClassification.PERMANENT
            )

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            retry_state["count"] = outcome.attempt
            now = int(time.time() * 1000)
            if outcome.will_retry:
                logger.info(
                    "transform_item_retry",
                    extra={"uid": uid, "item_id": item.item_id,
                           "retry": outcome.attempt, "delay_s": outcome.delay_seconds,
                           "error": str(exc)},
                )
                await self._error.record(
                    exc, uid=uid, pipeline=_TRANSFORM,
                    item_type=item.item_type, item_id=item.item_id,
                    retryable="true",
                    detail={"retry_count": outcome.attempt},
                )
                await self._data.put(key, {
                    "uid": uid, "pipeline": _TRANSFORM,
                    "item_type": item.item_type, "item_id": item.item_id,
                    "status": ProcessingItemStatus.FAILED.value,
                    "result": None,
                    "source_endpoints": list(handler.source_endpoints),
                    "processed_at": now,
                    "retry_count": outcome.attempt,
                })
                return None

            # Final failure: PERMANENT or RETRYABLE-exhausted.  No further
            # retry will happen, so retryable="false" is the correct label.
            await self._error.record(
                exc, uid=uid, pipeline=_TRANSFORM,
                item_type=item.item_type, item_id=item.item_id,
                retryable="false",
                detail=(
                    {"retry_count": outcome.attempt}
                    if outcome.attempt > 1 else None
                ),
            )
            await self._data.put(key, {
                "uid": uid, "pipeline": _TRANSFORM,
                "item_type": item.item_type, "item_id": item.item_id,
                "status": ProcessingItemStatus.FAILED.value,
                "result": None,
                "source_endpoints": list(handler.source_endpoints),
                "processed_at": now,
                "retry_count": outcome.attempt if outcome.attempt > 1 else 0,
            })
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=_classify,
        )
        driver = RetryDriver(policy)

        try:
            result = await driver.run(
                _do_transform, on_attempt_failed=_on_attempt_failed,
            )
        except Exception:  # noqa: BLE001 — final state already recorded
            return False

        now = int(time.time() * 1000)
        await self._data.put(key, {
            "uid": uid, "pipeline": _TRANSFORM,
            "item_type": item.item_type, "item_id": item.item_id,
            "status": ProcessingItemStatus.SUCCESS.value,
            "result": result,
            "source_endpoints": list(handler.source_endpoints),
            "processed_at": now,
        })
        return True
