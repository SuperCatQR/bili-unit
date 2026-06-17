# runner._endpoint — single uid-level endpoint execution logic.

from __future__ import annotations

import contextlib
import logging
import time
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
from .._adapter_core import extract_total_count
from ._failure import (
    FetchFailureState,
    classify_fetching_exception,
    record_endpoint_failure,
    record_unexpected_endpoint_failure,
)
from ._item_ids import _extract_item_ids_multi

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from .._store import FetchingStore

logger = logging.getLogger("bili.fetching.runner")


def _pagination_request_key(params: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Stable enough key for detecting repeated pagination requests."""
    return tuple(sorted((str(key), repr(value)) for key, value in params.items()))


def _stored_cursor_incomplete(
    stored_pages: list[Any],
    pagination_strategy: str,
) -> bool:
    if not stored_pages:
        return False
    last = stored_pages[-1]
    if not isinstance(last, dict):
        return False
    if pagination_strategy == "cursor":
        return bool(last.get("has_more"))
    if pagination_strategy == "anchor":
        return bool(last.get("anchor"))
    if pagination_strategy == "legacy_offset":
        return bool(last.get("has_more") == 1 and last.get("next_offset"))
    return False


def _stored_total_count(stored_pages: list[Any]) -> int:
    for page in stored_pages:
        if isinstance(page, dict):
            total = extract_total_count(page)
            if total > 0:
                return int(total)
    return 0


class _EndpointMixin:
    """Mixin providing ``_run_endpoint`` for :class:`Runner`.

    Accesses Runner state (``self._store``, ``self._rl``) and helper methods
    via the combined MRO at runtime.
    """

    _store: FetchingStore
    _fetch_fn: Any
    _rl: Any
    _settings: BiliSettings

    # -- single endpoint ---------------------------------------------------

    async def _run_endpoint(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        ep_name: str,
        credential: Any,
        mode: str = "incremental",
    ) -> None:
        """Run a uid-level endpoint with a safety net for unexpected errors."""
        try:
            await self._run_endpoint_inner(uid, spec, ep_name, credential, mode)
        except Exception as exc:  # noqa: BLE001 — defensive catch-all
            logger.exception(
                "endpoint_unexpected_error",
                extra={"uid": uid, "endpoint": ep_name, "error_type": type(exc).__name__},
            )
            try:
                err_id = await self._store.record_error(
                    endpoint=ep_name,
                    error_type="FetchingError",
                    message=f"unexpected: {type(exc).__name__}: {exc}",
                    retryable=False,
                )
            except Exception:  # noqa: BLE001 — must not mask the original failure
                err_id = None
            with contextlib.suppress(Exception):
                await self._store.update_endpoint_state(
                    ep_name,
                    status=EndpointStatus.FAILED_PERMANENT.value,
                    last_error_id=err_id,
                )

    async def _run_endpoint_inner(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        ep_name: str,
        credential: Any,
        mode: str = "incremental",
    ) -> None:
        # init
        await self._store.update_endpoint_state(
            ep_name,
            status=EndpointStatus.RUNNING.value,
            retry_count=0,
        )

        # -- incremental mode: build known_ids from stored data --
        known_ids: set[str] | None = None
        stored_total_count = 0
        stored_cursor_incomplete = False
        id_paths = spec.item_id_paths or ([spec.item_id_path] if spec.item_id_path else None)
        if mode in ("incremental", "refresh") and spec.pagination_strategy != "none":
            existing = await self._store.get_raw_payload(ep_name)
            if existing is not None and id_paths is not None:
                stored_pages = existing.get("pages", [])
                if isinstance(stored_pages, list):
                    stored_total_count = _stored_total_count(stored_pages)
                    stored_cursor_incomplete = _stored_cursor_incomplete(
                        stored_pages, spec.pagination_strategy,
                    )
                else:
                    stored_pages = []
                known_ids = set()
                for stored_page in stored_pages:
                    for item_id in _extract_item_ids_multi(stored_page, id_paths):
                        known_ids.add(item_id)
                logger.info(
                    "incremental_scan_started",
                    extra={"uid": uid, "endpoint": ep_name, "known_id_count": len(known_ids)},
                )
            else:
                # no stored data — fall back to full fetch
                logger.info(
                    "incremental_no_stored_data",
                    extra={"uid": uid, "endpoint": ep_name},
                )

        initial_known_count = len(known_ids) if known_ids is not None else 0

        # load progress (only for non-incremental or first-time incremental)
        progress = await self._store.get_progress(ep_name)
        request_params = spec.params_strategy.copy()
        if known_ids is not None:
            # incremental mode: always start from page 1, ignore stored progress
            request_params = spec.params_strategy.copy()
        elif progress is not None:
            cursor = progress.get("cursor")
            if cursor and isinstance(cursor, dict):
                # cursor stored as either dict (page params) or string token;
                # the store decodes JSON-shaped cursors back into dict form.
                # Plain string cursors are opaque to the runner — endpoints
                # with token cursors handle that themselves through fetch_fn.
                request_params = cursor

        settings = self._settings
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_fetching_retry_delays()

        retry_state = FetchFailureState()

        # track pages fetched in THIS run (for incremental overwrite)
        pages_this_run: list[dict[str, Any]] = []
        boundary_hit = False  # set when all IDs on a page are known
        pagination_loop_detected = False
        seen_page_requests: set[tuple[tuple[str, str], ...]] = set()

        async def _fetch_one_page(params: dict[str, Any]):
            await self._rl.acquire(spec.rate_limit_key)
            return await self._fetch_fn(
                uid, spec, credential, params,
                timeout=settings.bili_fetching_request_timeout,
            )

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            if isinstance(exc, AuthError):
                await record_endpoint_failure(
                    self._store,
                    endpoint=ep_name,
                    status=EndpointStatus.FAILED_PERMANENT,
                    state=retry_state,
                    exc=exc,
                    retryable=False,
                )
                return None

            if isinstance(exc, ResourceUnavailableError):
                await record_endpoint_failure(
                    self._store,
                    endpoint=ep_name,
                    status=EndpointStatus.FAILED_PERMANENT,
                    state=retry_state,
                    exc=exc,
                    retryable=False,
                )
                logger.info(
                    "endpoint_unavailable",
                    extra={"uid": uid, "endpoint": ep_name, "reason": str(exc)},
                )
                return None

            if isinstance(exc, Http412Error):
                advice = await self._rl.record_412(spec.rate_limit_key)
                # rate-limit state is in-memory only (locked decision §11);
                # no persistence call here.
                retry_count = retry_state.bump()
                detail = {
                    "retry_count": retry_count,
                    "params": request_params,
                }
                await record_endpoint_failure(
                    self._store,
                    endpoint=ep_name,
                    status=EndpointStatus.FAILED_RETRYABLE,
                    state=retry_state,
                    exc=exc,
                    retryable=outcome.will_retry,
                    detail=detail,
                )
                if not outcome.will_retry:
                    await self._store.update_endpoint_state(
                        ep_name,
                        status=EndpointStatus.FAILED_EXHAUSTED.value,
                        retry_count=retry_state.count,
                        last_error_id=retry_state.last_error_id,
                    )
                    return None
                wait = max(advice.get("wait_seconds", 0), outcome.delay_seconds)
                logger.info(
                    "retry_scheduled",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "wait_s": wait, "retry": retry_state.count,
                    },
                )
                return wait

            if isinstance(exc, FetchingError):
                retry_count = retry_state.bump()
                await record_endpoint_failure(
                    self._store,
                    endpoint=ep_name,
                    status=EndpointStatus.FAILED_RETRYABLE,
                    state=retry_state,
                    exc=exc,
                    retryable=outcome.will_retry,
                )
                if not outcome.will_retry:
                    await self._store.update_endpoint_state(
                        ep_name,
                        status=EndpointStatus.FAILED_EXHAUSTED.value,
                        retry_count=retry_state.count,
                        last_error_id=retry_state.last_error_id,
                    )
                    return None
                logger.info(
                    "retry_scheduled",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "wait_s": outcome.delay_seconds,
                        "retry": retry_count,
                    },
                )
                return None

            # Unexpected non-fetching error — wrap and treat as permanent.
            await record_unexpected_endpoint_failure(
                self._store,
                endpoint=ep_name,
                state=retry_state,
                exc=exc,
            )
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=classify_fetching_exception,
        )
        driver = RetryDriver(policy)

        while True:
            if spec.pagination_strategy != "none":
                request_key = _pagination_request_key(request_params)
                if request_key in seen_page_requests:
                    pagination_loop_detected = True
                    logger.warning(
                        "pagination_loop_detected",
                        extra={
                            "uid": uid,
                            "endpoint": ep_name,
                            "request_params": request_params,
                        },
                    )
                    break
                seen_page_requests.add(request_key)

            try:
                page = await driver.run(
                    lambda params=request_params: _fetch_one_page(params),
                    on_attempt_failed=_on_attempt_failed,
                )
            except Exception:
                # Final state already written by _on_attempt_failed.
                return

            # success — track page
            now_ms = int(time.time() * 1000)
            pages_this_run.append(page.raw_payload)

            # -- save progress for non-incremental pagination (resume support) --
            if known_ids is None and spec.pagination_strategy != "none":
                await self._store.save_progress(
                    ep_name,
                    {
                        "cursor": page.next_request,
                        "total": None,
                        "fetched": None,
                    },
                )

            # -- incremental mode: check item IDs --
            if known_ids is not None and spec.pagination_strategy != "none" and id_paths is not None:
                page_ids = _extract_item_ids_multi(page.raw_payload, id_paths)
                new_ids = set(page_ids) - known_ids if page_ids else set()
                logger.info(
                    "incremental_page_checked",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "new_count": len(new_ids),
                        "known_count": len(page_ids) - len(new_ids),
                        "total_page_ids": len(page_ids),
                    },
                )
                if page_ids and not new_ids:
                    page_total_count = extract_total_count(page.raw_payload)
                    total_count = page_total_count or stored_total_count
                    incomplete_listing = (
                        total_count > 0 and len(known_ids) < total_count
                    ) or stored_cursor_incomplete
                    if incomplete_listing and not page.is_last_page:
                        logger.info(
                            "incremental_backfill_incomplete",
                            extra={
                                "uid": uid,
                                "endpoint": ep_name,
                                "known_id_count": len(known_ids),
                                "total_count": total_count,
                                "stored_cursor_incomplete": stored_cursor_incomplete,
                            },
                        )
                    else:
                        boundary_hit = True
                        logger.info(
                            "incremental_boundary_hit",
                            extra={"uid": uid, "endpoint": ep_name},
                        )
                        if not page.is_last_page:
                            safety_params = page.next_request or request_params
                            try:
                                safety_page = await _fetch_one_page(safety_params)
                                pages_this_run.append(safety_page.raw_payload)
                                logger.info(
                                    "incremental_safety_page",
                                    extra={"uid": uid, "endpoint": ep_name},
                                )
                            except Exception as exc:
                                logger.warning(
                                    "incremental_safety_page_failed",
                                    extra={
                                        "uid": uid, "endpoint": ep_name,
                                        "error": str(exc),
                                    },
                                )
                        break

                known_ids.update(str(i) for i in new_ids)

            if page.is_last_page or spec.pagination_strategy == "none":
                break

            next_params = page.next_request or request_params
            if spec.pagination_strategy != "none":
                next_key = _pagination_request_key(next_params)
                if next_key in seen_page_requests:
                    pagination_loop_detected = True
                    logger.warning(
                        "pagination_loop_detected",
                        extra={
                            "uid": uid,
                            "endpoint": ep_name,
                            "request_params": next_params,
                        },
                    )
                    break

            request_params = next_params

        # -- store results --
        if spec.pagination_strategy != "none":
            if known_ids is not None:
                # incremental: overwrite with pages from this run only
                raw_payload: dict[str, Any] = {"pages": pages_this_run}
            elif mode == "full":
                # full mode: overwrite entirely (do NOT accumulate)
                raw_payload = {"pages": pages_this_run}
            else:
                # incremental first run (no stored data): accumulate on existing
                existing = await self._store.get_raw_payload(ep_name)
                pages = (existing or {}).get("pages", [])
                pages.extend(pages_this_run)
                raw_payload = {"pages": pages}
        else:
            raw_payload = pages_this_run[0] if pages_this_run else page.raw_payload

        # build next progress
        if spec.pagination_strategy != "none":
            if page.is_last_page or boundary_hit or pagination_loop_detected:
                next_progress: dict[str, Any] | None = {
                    "cursor": None,
                    "total": None,
                    "fetched": None,
                }
            else:
                next_progress = {
                    "cursor": page.next_request,
                    "total": None,
                    "fetched": None,
                }
        else:
            next_progress = None

        # transactional write: payload + progress in one transaction when both
        if next_progress is not None:
            await self._store.save_raw_page_and_progress(
                ep_name, "", raw_payload, next_progress, fetched_at_ms=now_ms,
            )
        else:
            await self._store.save_raw_payload(
                ep_name, "", raw_payload, fetched_at_ms=now_ms,
            )

        if known_ids is not None:
            logger.info(
                "incremental_completed",
                extra={
                    "uid": uid, "endpoint": ep_name,
                    "total_pages_fetched": len(pages_this_run),
                    "new_item_count": len(known_ids) - initial_known_count,
                    "mode": "incremental",
                },
            )

        logger.info(
            "endpoint_page_saved",
            extra={"uid": uid, "endpoint": ep_name},
        )

        await self._store.update_endpoint_state(
            ep_name,
            status=EndpointStatus.SUCCESS.value,
            retry_count=retry_state.count,
        )
