# runner._transform — transform pipeline (scan → enqueue → workers → rollup).
#
# After the parsing-layer migration, transform items are discovered from
# the parsing store (ParsingQuery), not directly from fetching raw payloads.
# Each handler's transform() receives a WorkItem whose item_data is a typed-
# object dict from the parsing store.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .. import (
    ProcessingError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
)
from ..keys import _proc_key, _task_key
from ..task import PipelineEntry, ProcessingTaskValue
from ..transform import get_handler
from ..transform._base import WorkItem
from ._pipeline_executor import (
    ItemRetryContext,
    WorkerOutcome,
    run_item_with_retry,
    run_item_workers,
)

if TYPE_CHECKING:
    from ...parsing.query import ParsingQuery
    from ..env import ProcessingEnv

logger = logging.getLogger("bili.processing.runner")


_TRANSFORM = "transform"


class _TransformMixin:
    """Mixin providing transform pipeline methods for :class:`ProcessingRunner`.

    Accesses runner state (``self._data``, ``self._error``, ``self._parse_qry``,
    ``self._settings``) and helpers (``_write_progress``, ``_derive_pipeline_status``,
    ``_is_retryable``) via the combined MRO at runtime.
    """

    _data: Any
    _error: Any
    _parse_qry: ParsingQuery | None
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

        Returns (ready_items, skipped_count).  Reads typed-object dicts from
        the parsing store via ``self._parse_qry``.
        """
        if self._parse_qry is None:
            logger.warning(
                "parsing_query_unavailable",
                extra={"uid": uid, "item_type": handler.item_type},
            )
            return [], 0

        items: list[WorkItem] = await self._read_parsing_items(uid, handler.item_type)
        return await self._filter_ready(uid, handler, items, mode)

    async def _read_parsing_items(
        self: Any,
        uid: int,
        item_type: str,
    ) -> list[WorkItem]:
        """Read typed-object dicts from the parsing store for one item_type."""
        pq = self._parse_qry

        if item_type == "user_profile":
            d = await pq.get_user_profile(uid)
            if d is None:
                return []
            return [WorkItem(item_type=item_type, item_id=str(uid), item_data=d)]

        if item_type == "video_metadata":
            rows = await pq.list_video_details(uid)
            return [
                WorkItem(item_type=item_type, item_id=r.get("bvid", ""), item_data=r)
                for r in rows if isinstance(r, dict) and r.get("bvid")
            ]

        if item_type == "articles":
            rows = await pq.list_articles(uid)
            return [
                WorkItem(item_type=item_type, item_id=str(r.get("id", "")), item_data=r)
                for r in rows if isinstance(r, dict) and r.get("id")
            ]

        if item_type == "opus":
            rows = await pq.list_opus(uid)
            return [
                WorkItem(item_type=item_type, item_id=str(r.get("id", "")), item_data=r)
                for r in rows if isinstance(r, dict) and r.get("id")
            ]

        if item_type == "dynamics":
            rows = await pq.list_dynamics(uid)
            return [
                WorkItem(item_type=item_type, item_id=str(r.get("id_str", "")), item_data=r)
                for r in rows if isinstance(r, dict) and r.get("id_str")
            ]

        logger.warning(
            "unknown_item_type",
            extra={"uid": uid, "item_type": item_type},
        )
        return []

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

        async def process_item(item: WorkItem) -> WorkerOutcome:
            handler = get_handler(item.item_type)
            if handler is None:
                return WorkerOutcome(
                    bucket=item.item_type,
                    failed=1,
                    postfix=f"skip {item.item_type}",
                )

            ok = await self._process_one(uid, handler, item)
            return WorkerOutcome(
                bucket=item.item_type,
                completed=1 if ok else 0,
                failed=0 if ok else 1,
                postfix=f"{item.item_type}/{item.item_id} {'ok' if ok else 'fail'}",
            )

        await run_item_workers(
            items=items,
            worker_count=worker_count,
            queue_maxsize=int(self._settings.bili_processing_queue_maxsize),
            label=f"transform uid={uid}",
            rollup=rollup,
            process_item=process_item,
        )

    async def _process_one(
        self: Any,
        uid: int,
        handler: Any,
        item: WorkItem,
    ) -> bool:
        async def _do_transform():
            return handler.transform(item)

        ctx = ItemRetryContext(
            uid=uid,
            pipeline=_TRANSFORM,
            item_type=item.item_type,
            item_id=item.item_id,
            source_endpoints=tuple(handler.source_endpoints),
            key=_proc_key(uid, item.item_type, item.item_id),
            retry_event="transform_item_retry",
            log_id_field="item_id",
            log_id_value=item.item_id,
        )
        return await run_item_with_retry(
            ctx,
            data=self._data,
            error=self._error,
            do_work=_do_transform,
            is_retryable=self._is_retryable,
            max_attempts=self._settings.bili_processing_max_retries + 1,
            delays=self._settings.get_retry_delays(),
            logger=logger,
        )
