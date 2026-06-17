# runner._pipeline_executor -- shared execution mechanics for processing.
#
# Pipeline-specific discovery lives in the audio mixin (and future pipelines
# such as subtitle/OCR).  This
# module owns the parts every pipeline does identically:
#
#   * queue fan-out, progress display, locked rollup updates
#     (``run_item_workers`` + ``WorkerOutcome``)
#   * per-item retry + error recording + status persistence
#     (``run_item_with_retry`` + ``ItemRetryContext``)
#
# Phase 3.3: ``data`` / ``error`` are gone — the executor talks to a single
# :class:`ProcessingStore`. Persistence dispatch is pipeline-specific so the
# store call sites are typed (audio → ``save_audio_transcription``); when a
# second pipeline lands, the seam grows a new branch.

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..._logging import Progress
from ..._retry import (
    RetryClassification,
    RetryDriver,
    RetryOutcome,
    RetryPolicy,
)
from .. import ProcessingItemStatus

if TYPE_CHECKING:
    from .._store import ProcessingStore


@dataclass(frozen=True)
class WorkItem:
    """A single processing work unit, addressable by (item_type, item_id).

    item_data carries the typed-object dict (e.g. a VideoDetail serialised
    via to_dict()).  Keeping this self-contained makes the work call a pure
    function.
    """

    item_type: str
    item_id: str
    item_data: dict[str, Any]


@dataclass(frozen=True)
class WorkerOutcome:
    """Counters and progress text produced by one processed work item."""

    bucket: str
    postfix: str
    completed: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ItemRetryContext:
    """Everything that differs between pipelines for one retried work item.

    The executor (:func:`run_item_with_retry`) owns the retry orchestration
    and the success/failure write skeleton; this context carries the
    pipeline-specific identity and the log event names.
    """

    uid: int
    pipeline: str
    item_type: str
    item_id: str
    source_endpoints: tuple[str, ...]
    # Logging — pipelines key log lines on their preferred id field
    # (audio uses ``bvid``).  ``failed_event`` may be None when a pipeline
    # logs nothing on final failure.
    retry_event: str
    log_id_field: str
    log_id_value: str
    failed_event: str | None = None


class ItemPersistence(Protocol):
    async def save_failure(
        self,
        store: ProcessingStore,
        ctx: ItemRetryContext,
        record: dict[str, Any],
    ) -> None: ...

    async def save_success(
        self,
        store: ProcessingStore,
        ctx: ItemRetryContext,
        record: dict[str, Any],
        result: Any,
    ) -> None: ...


def _build_record_payload(
    ctx: ItemRetryContext,
    status: str,
    result: Any,
    processed_at_ms: int,
    *,
    retry_count: int | None = None,
) -> dict[str, Any]:
    """Build the ProcessingItemDTO-shaped dict stored in ``audio_transcription.payload``."""
    payload: dict[str, Any] = {
        "uid": ctx.uid,
        "pipeline": ctx.pipeline,
        "item_type": ctx.item_type,
        "item_id": ctx.item_id,
        "status": status,
        "result": result,
        "source_endpoints": list(ctx.source_endpoints),
        "processed_at": processed_at_ms,
    }
    if retry_count is not None:
        payload["retry_count"] = retry_count
    return payload


class AudioItemPersistence:
    """Persist audio pipeline item success/failure rows."""

    async def save_failure(
        self,
        store: ProcessingStore,
        ctx: ItemRetryContext,
        record: dict[str, Any],
    ) -> None:
        await store.save_audio_transcription(
            ctx.item_id,
            status="failed",
            transcription_source=None,
            transcript=None,
            audio_tokens=None,
            seconds=None,
            cache_hits=None,
            payload=record,
        )

    async def save_success(
        self,
        store: ProcessingStore,
        ctx: ItemRetryContext,
        record: dict[str, Any],
        result: Any,
    ) -> None:
        if not isinstance(result, dict):
            result = {}
        transcription_source = result.get("transcription_source")
        pages = result.get("pages") or []
        transcript = "\n".join(
            p.get("text", "") for p in pages if isinstance(p, dict)
        )
        cost = result.get("cost") or {}
        audio_tokens = cost.get("audio_tokens")
        seconds = cost.get("seconds")
        cache_hits = cost.get("cache_hits")
        await store.save_audio_transcription(
            ctx.item_id,
            status="success",
            transcription_source=transcription_source,
            transcript=transcript or None,
            audio_tokens=int(audio_tokens) if audio_tokens is not None else None,
            seconds=float(seconds) if seconds is not None else None,
            cache_hits=int(cache_hits) if cache_hits is not None else None,
            payload=record,
        )


async def run_item_with_retry(
    ctx: ItemRetryContext,
    *,
    store: ProcessingStore,
    persistence: ItemPersistence,
    do_work: Callable[[], Awaitable[Any]],
    is_retryable: Callable[[Exception], bool],
    max_attempts: int,
    delays: list[int],
    logger: logging.Logger,
) -> bool:
    """Process one work item: retry → record errors → persist status.

    Shared retry/record/persist skeleton for every processing pipeline.
    On every failed attempt the matching error is recorded and a FAILED
    status row is written; on success a SUCCESS row carrying ``result`` is
    written.  Returns True on success, False once retries are exhausted or a
    PERMANENT error is hit (final state is already persisted either way).
    """

    def _classify(exc: Exception) -> RetryClassification:
        return (
            RetryClassification.RETRYABLE
            if is_retryable(exc)
            else RetryClassification.PERMANENT
        )

    async def _on_attempt_failed(
        exc: Exception, outcome: RetryOutcome,
    ) -> int | None:
        now = int(time.time() * 1000)
        if outcome.will_retry:
            logger.info(
                ctx.retry_event,
                extra={"uid": ctx.uid, ctx.log_id_field: ctx.log_id_value,
                       "retry": outcome.attempt,
                       "delay_s": outcome.delay_seconds, "error": str(exc)},
            )
            await store.record_error(
                pipeline=ctx.pipeline,
                item_type=ctx.item_type,
                item_id=ctx.item_id,
                error_type=type(exc).__name__,
                message=str(exc),
                retryable=True,
                detail={"retry_count": outcome.attempt},
            )
            record = _build_record_payload(
                ctx, ProcessingItemStatus.FAILED.value, None, now,
                retry_count=outcome.attempt,
            )
            await persistence.save_failure(store, ctx, record)
            return None

        # Final failure: PERMANENT or RETRYABLE-exhausted.
        if ctx.failed_event is not None:
            logger.warning(
                ctx.failed_event,
                extra={"uid": ctx.uid, ctx.log_id_field: ctx.log_id_value,
                       "retry_count": outcome.attempt, "error": str(exc)},
            )
        await store.record_error(
            pipeline=ctx.pipeline,
            item_type=ctx.item_type,
            item_id=ctx.item_id,
            error_type=type(exc).__name__,
            message=str(exc),
            retryable=False,
            detail=(
                {"retry_count": outcome.attempt}
                if outcome.attempt > 1 else None
            ),
        )
        record = _build_record_payload(
            ctx, ProcessingItemStatus.FAILED.value, None, now,
            retry_count=outcome.attempt if outcome.attempt > 1 else 0,
        )
        await persistence.save_failure(store, ctx, record)
        return None

    policy = RetryPolicy(
        max_attempts=max_attempts, delays=delays, classify=_classify,
    )
    driver = RetryDriver(policy)

    try:
        result = await driver.run(do_work, on_attempt_failed=_on_attempt_failed)
    except Exception:  # noqa: BLE001 — final state already recorded
        return False

    now = int(time.time() * 1000)
    record = _build_record_payload(
        ctx, ProcessingItemStatus.SUCCESS.value, result, now,
    )
    await persistence.save_success(store, ctx, record, result)
    return True


async def run_item_workers(
    *,
    items: list[WorkItem],
    worker_count: int,
    queue_maxsize: int,
    label: str,
    rollup: dict[str, dict[str, int]],
    process_item: Callable[[WorkItem], Awaitable[WorkerOutcome]],
) -> None:
    """Run a bounded worker pool and update ``rollup`` from item outcomes."""
    worker_count = max(1, worker_count)
    queue: asyncio.Queue[WorkItem | None] = asyncio.Queue(
        maxsize=max(1, queue_maxsize),
    )
    rollup_lock = asyncio.Lock()
    bar = Progress(total=len(items), label=label)

    async def producer() -> None:
        for item in items:
            await queue.put(item)
        for _ in range(worker_count):
            await queue.put(None)

    async def worker(idx: int) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return

            outcome = await process_item(item)
            async with rollup_lock:
                bucket = rollup.setdefault(
                    outcome.bucket,
                    {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                )
                bucket["completed"] += outcome.completed
                bucket["failed"] += outcome.failed
                bucket["skipped"] += outcome.skipped

            bar.update(1, postfix=outcome.postfix)

    try:
        await asyncio.gather(
            producer(),
            *[worker(i) for i in range(worker_count)],
        )
    finally:
        bar.close()
