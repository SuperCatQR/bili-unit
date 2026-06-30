# runner._item_fanout — item-level fan-out endpoint execution logic.

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..._retry import RetryDriver, RetryOutcome, RetryPolicy
from .. import (
    AuthError,
    EndpointStatus,
    FetchingError,
    Http412Error,
    ResourceUnavailableError,
)
from .._endpoint_spec import EndpointSpec
from ._failure import (
    _emit_failed,
    _emit_retry_scheduled,
    _emit_unavailable,
    _emit_unexpected_failed,
    _log_exhausted,
    _log_retry_scheduled,
    _log_unavailable,
    classify_fetching_exception,
)

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from .._store import FetchingStore

logger = logging.getLogger("bili.fetching.runner")


class _ItemFanoutResult(StrEnum):
    """Outcome of _process_single_item for a single item."""
    SUCCESS = "success"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    PERMANENT = "permanent"  # AuthError — aborts whole fan-out


@dataclass(frozen=True)
class _ItemFanoutPlan:
    """Execution plan for one item-level endpoint run."""

    total: int
    to_fetch: list[str]
    skipped_existing: int = 0
    skipped_fresh: int = 0
    skipped_unavailable: int = 0
    skipped_filtered: int = 0

    @property
    def skipped(self) -> int:
        return (
            self.skipped_existing
            + self.skipped_fresh
            + self.skipped_unavailable
            + self.skipped_filtered
        )


class _ItemFanoutMixin:
    """Mixin providing ``_run_item_endpoint`` / ``_process_single_item`` for :class:`Runner`."""

    _store: FetchingStore
    _rl: Any
    _settings: BiliSettings
    _progress_factory: Any

    # -- item-level fan-out endpoint ------------------------------------

    async def _run_item_endpoint(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        credential: Any,
        mode: str = "incremental",
        *,
        show_progress: bool = True,
    ) -> None:
        """Execute an item-level fan-out endpoint (e.g. video_detail).

        Reads items from the source endpoint's stored data, fetches each item
        individually (up to ``item_concurrency`` in parallel), and stores
        results per item_id.
        """
        ep_name = spec.name
        reporter = getattr(self, "_reporter", None)
        if reporter is not None:
            await reporter.emit(
                "fetch.endpoint.started",
                stage="fetching",
                endpoint=ep_name,
                data={
                    "kind": spec.kind,
                    "mode": mode,
                    "source_endpoint": spec.source_endpoint,
                },
            )
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
            if reporter is not None:
                await reporter.emit(
                    "fetch.endpoint.failed",
                    stage="fetching",
                    level="ERROR",
                    endpoint=ep_name,
                    data={
                        "status": EndpointStatus.FAILED_PERMANENT.value,
                        "source_endpoint": spec.source_endpoint,
                        "last_error_id": err_id,
                    },
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
            if reporter is not None:
                await reporter.emit(
                    "fetch.endpoint.completed",
                    stage="fetching",
                    endpoint=ep_name,
                    data={"status": EndpointStatus.SUCCESS.value, "total": 0},
                )
            return

        plan = await self._build_item_fanout_plan(ep_name, items, mode, spec)
        total_items = plan.total
        completed_items = plan.skipped
        failed_items = 0
        unavailable_items = 0

        settings = self._settings
        max_concurrent = max(settings.bili_fetching_item_concurrency, 1)
        semaphore = asyncio.Semaphore(max_concurrent)

        bar = self._progress_factory(
            total=total_items,
            label=f"fetch uid={uid} {ep_name}",
            enabled=show_progress,
            emit_summary=show_progress,
        )

        async def process_item(item_id: str) -> str:
            nonlocal completed_items, failed_items, unavailable_items
            async with semaphore:
                result = await self._process_single_item(
                    uid, spec, credential, item_id, mode,
                )
            if result == _ItemFanoutResult.SUCCESS:
                completed_items += 1
            elif result == _ItemFanoutResult.UNAVAILABLE:
                completed_items += 1
                unavailable_items += 1
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
                    "skipped": plan.skipped + unavailable_items,
                    "skipped_existing": plan.skipped_existing,
                    "skipped_fresh": plan.skipped_fresh,
                    "skipped_unavailable": (
                        plan.skipped_unavailable + unavailable_items
                    ),
                    "skipped_filtered": plan.skipped_filtered,
                },
            )
            return result

        # 3. Run all items concurrently (bounded by semaphore).
        try:
            try:
                if plan.to_fetch:
                    await asyncio.gather(
                        *[process_item(iid) for iid in plan.to_fetch],
                        return_exceptions=False,
                    )
            except FetchingError:
                # AuthError from any item → abort and mark permanently failed.
                await self._store.update_endpoint_state(
                    ep_name,
                    status=EndpointStatus.FAILED_PERMANENT.value,
                )
                if reporter is not None:
                    await reporter.emit(
                        "fetch.endpoint.failed",
                        stage="fetching",
                        level="ERROR",
                        endpoint=ep_name,
                        data={"status": EndpointStatus.FAILED_PERMANENT.value},
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

        skipped_unavailable = plan.skipped_unavailable + unavailable_items
        skipped_total = plan.skipped + unavailable_items

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
                "skipped": skipped_total,
                "skipped_existing": plan.skipped_existing,
                "skipped_fresh": plan.skipped_fresh,
                "skipped_unavailable": skipped_unavailable,
                "skipped_filtered": plan.skipped_filtered,
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
                "skipped": skipped_total,
                "skipped_existing": plan.skipped_existing,
                "skipped_fresh": plan.skipped_fresh,
                "skipped_unavailable": skipped_unavailable,
                "skipped_filtered": plan.skipped_filtered,
            },
        )
        if reporter is not None:
            event = (
                "fetch.endpoint.completed"
                if final_status in (EndpointStatus.SUCCESS, EndpointStatus.PARTIAL_ITEM)
                else "fetch.endpoint.failed"
            )
            await reporter.emit(
                event,
                stage="fetching",
                level=(
                    "INFO"
                    if final_status == EndpointStatus.SUCCESS
                    else "WARNING"
                ),
                endpoint=ep_name,
                data={
                    "status": final_status.value,
                    "completed": completed_items,
                    "failed": failed_items,
                    "total": total_items,
                    "skipped": skipped_total,
                    "skipped_existing": plan.skipped_existing,
                    "skipped_fresh": plan.skipped_fresh,
                    "skipped_unavailable": skipped_unavailable,
                    "skipped_filtered": plan.skipped_filtered,
                },
            )

    async def _build_item_fanout_plan(
        self: Any,
        ep_name: str,
        items: list[str],
        mode: str,
        spec: EndpointSpec,
    ) -> _ItemFanoutPlan:
        """Classify fan-out items before execution."""
        original_total = len(items)
        filtered_out = 0
        if spec.skip_item is not None and spec.source_endpoint:
            source_payload = await self._store.get_raw_payload(spec.source_endpoint)
            if source_payload:
                filtered_items: list[str] = []
                for item_id in items:
                    source_item = self._find_source_item(source_payload, spec.source_endpoint, item_id)
                    if source_item is not None:
                        reason = spec.skip_item(source_item)
                        if reason is not None:
                            filtered_out += 1
                            logger.info(
                                "item_endpoint_item_filtered",
                                extra={
                                    "endpoint": ep_name,
                                    "item_id": item_id,
                                    "reason": reason,
                                },
                            )
                            continue
                    filtered_items.append(item_id)
                items = filtered_items
        if mode == "full":
            return _ItemFanoutPlan(
                total=original_total,
                to_fetch=list(items),
                skipped_filtered=filtered_out,
            )

        completed = set(await self._store.list_completed_items(ep_name))
        unavailable = set(await self._store.list_unavailable_items(ep_name))
        item_ages = (
            await self._store.list_item_ages_ms(ep_name)
            if mode == "refresh"
            else {}
        )
        now_ms = int(time.time() * 1000)
        threshold_ms = (
            self._settings.bili_fetching_refresh_after_days * 86400 * 1000
        )

        to_fetch: list[str] = []
        skipped_existing = 0
        skipped_fresh = 0
        skipped_unavailable = 0
        for item_id in items:
            if item_id in unavailable:
                skipped_unavailable += 1
                continue
            if item_id in completed:
                if mode == "refresh":
                    fetched_at = item_ages.get(item_id)
                    if fetched_at is not None and now_ms - fetched_at >= threshold_ms:
                        to_fetch.append(item_id)
                    else:
                        skipped_fresh += 1
                else:
                    skipped_existing += 1
                continue
            to_fetch.append(item_id)

        return _ItemFanoutPlan(
            total=original_total,
            to_fetch=to_fetch,
            skipped_existing=skipped_existing,
            skipped_fresh=skipped_fresh,
            skipped_unavailable=skipped_unavailable,
            skipped_filtered=filtered_out,
        )

    def _find_source_item(
        self: Any,
        source_payload: dict[str, Any],
        source_endpoint: str,
        item_id: str,
    ) -> dict[str, Any] | None:
        pages = source_payload.get("pages")
        if source_endpoint == "articles" and isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                articles = page.get("articles")
                if not isinstance(articles, list):
                    continue
                for article in articles:
                    if (
                        isinstance(article, dict)
                        and str(article.get("id")) == item_id
                    ):
                        return article
        if source_endpoint == "opus" and isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                items = page.get("items")
                if not isinstance(items, list):
                    continue
                for opus_item in items:
                    if (
                        isinstance(opus_item, dict)
                        and str(opus_item.get("opus_id")) == item_id
                    ):
                        return opus_item
        return None

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

        settings = self._settings
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_fetching_retry_delays()
        retry_state = {"count": 0}

        worker = getattr(self, "_worker", None)

        async def _do_fetch():
            await self._rl.acquire(spec.rate_limit_key)
            extra_kw: dict = {"timeout": settings.bili_fetching_request_timeout}
            if spec.needs_parent_uid:
                extra_kw["_uid"] = uid
            if worker is not None:
                # Worker path: route item fetch through IPC, unwrap the
                # ``{"raw_payload": ...}`` envelope. Worker-side business errors
                # arrive re-raised as FetchingError subclasses (via the error
                # pack), so they flow through the same RetryDriver +
                # _on_attempt_failed + three-state classification as the
                # in-process path below — keeping retry/limit/PERMANENT vs
                # UNAVAILABLE vs FAILED behaviour identical across both paths.
                env = await worker.fetch_item(
                    item_id, spec.name, worker.credential_ref, extra_kw,
                )
                return env["raw_payload"]
            return await spec.callable(item_id, credential, **extra_kw)

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            reporter = getattr(self, "_reporter", None)
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
                _log_unavailable(logger, namespace="fetch.item", uid=uid,
                                 ep_name=ep_name, item_id=item_id, reason=str(exc))
                await _emit_unavailable(reporter, namespace="fetch.item",
                                        ep_name=ep_name, item_id=item_id, exc=exc)
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
                    _log_exhausted(logger, namespace="fetch.item", uid=uid,
                                   ep_name=ep_name, item_id=item_id,
                                   retry=retry_state["count"])
                    await _emit_failed(reporter, namespace="fetch.item",
                                       ep_name=ep_name, item_id=item_id, exc=exc,
                                       retry=retry_state["count"])
                    return None
                wait = max(advice.get("wait_seconds", 0), outcome.delay_seconds)
                _log_retry_scheduled(logger, namespace="fetch.item", uid=uid,
                                     ep_name=ep_name, item_id=item_id,
                                     retry=retry_state["count"], wait_s=wait)
                await _emit_retry_scheduled(reporter, namespace="fetch.item",
                                            ep_name=ep_name, item_id=item_id, exc=exc,
                                            retry=retry_state["count"], delay_s=wait)
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
                    _log_exhausted(logger, namespace="fetch.item", uid=uid,
                                   ep_name=ep_name, item_id=item_id,
                                   retry=retry_state["count"], reason=str(exc))
                    await _emit_failed(reporter, namespace="fetch.item",
                                       ep_name=ep_name, item_id=item_id, exc=exc,
                                       retry=retry_state["count"])
                    return None
                _log_retry_scheduled(logger, namespace="fetch.item", uid=uid,
                                     ep_name=ep_name, item_id=item_id,
                                     retry=retry_state["count"],
                                     wait_s=outcome.delay_seconds, reason=str(exc))
                await _emit_retry_scheduled(reporter, namespace="fetch.item",
                                            ep_name=ep_name, item_id=item_id, exc=exc,
                                            retry=retry_state["count"],
                                            delay_s=outcome.delay_seconds)
                return None

            await self._store.record_error(
                endpoint=ep_name,
                error_type="FetchingError",
                message=f"unexpected: {type(exc).__name__}: {exc}",
                retryable=False,
                detail={"item_id": item_id},
            )
            await _emit_unexpected_failed(reporter, namespace="fetch.item",
                                          ep_name=ep_name, item_id=item_id, exc=exc)
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
        except ResourceUnavailableError:
            return _ItemFanoutResult.UNAVAILABLE
        except Exception:
            return _ItemFanoutResult.FAILED

        # Success — store item.
        await self._store.save_raw_payload(ep_name, item_id, result)
        logger.info(
            "item_endpoint_item_saved",
            extra={"uid": uid, "endpoint": ep_name, "item_id": item_id},
        )
        reporter = getattr(self, "_reporter", None)
        if reporter is not None:
            await reporter.emit(
                "fetch.item.saved",
                stage="fetching",
                endpoint=ep_name,
                item_type=ep_name,
                item_id=item_id,
            )
        return _ItemFanoutResult.SUCCESS
