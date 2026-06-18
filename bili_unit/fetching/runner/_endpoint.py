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
from .._adapter_core import extract_total_count
from .._endpoint_spec import EndpointSpec
from ._failure import (
    FetchFailureState,
    _emit_retry_scheduled,
    _emit_unavailable,
    _log_retry_scheduled,
    _log_unavailable,
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


def _merge_incremental_pages(
    *,
    new_pages: list[dict[str, Any]],
    stored_pages: list[Any],
    id_paths: list[str] | None,
) -> list[Any]:
    """Keep fresh pages first while preserving old unseen listing pages."""
    if not id_paths:
        return list(new_pages)
    seen_ids: set[str] = set()
    merged: list[Any] = []
    for page in new_pages:
        page_ids = set(_extract_item_ids_multi(page, id_paths))
        if page_ids and page_ids <= seen_ids:
            continue
        seen_ids.update(page_ids)
        merged.append(page)
    for stored_page in stored_pages:
        if not isinstance(stored_page, dict):
            merged.append(stored_page)
            continue
        stored_ids = set(_extract_item_ids_multi(stored_page, id_paths))
        if stored_ids and stored_ids <= seen_ids:
            continue
        seen_ids.update(stored_ids)
        merged.append(stored_page)
    return merged


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
        reporter = getattr(self, "_reporter", None)
        if reporter is not None:
            await reporter.emit(
                "fetch.endpoint.started",
                stage="fetching",
                endpoint=ep_name,
                data={"kind": spec.kind, "mode": mode},
            )
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
            if reporter is not None:
                await reporter.emit(
                    "fetch.endpoint.failed",
                    stage="fetching",
                    level="ERROR",
                    endpoint=ep_name,
                    data={
                        "status": EndpointStatus.FAILED_PERMANENT.value,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "last_error_id": err_id,
                    },
                )
            return

        if reporter is None:
            return
        state = await self._store.get_endpoint_state(ep_name) or {}
        status = state.get("status")
        data = {
            "status": status,
            "retry_count": state.get("retry_count"),
            "last_error_id": state.get("last_error_id"),
        }
        if status == EndpointStatus.SUCCESS.value:
            await reporter.emit(
                "fetch.endpoint.completed",
                stage="fetching",
                endpoint=ep_name,
                data=data,
            )
        else:
            await reporter.emit(
                "fetch.endpoint.failed",
                stage="fetching",
                level="WARNING",
                endpoint=ep_name,
                data=data,
            )

    async def _build_known_ids_for_incremental(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        ep_name: str,
        mode: str,
        progress_cursor: Any,
    ) -> tuple[set[str] | None, int, bool, list[Any]]:
        """Build the known-ID set from stored pages for incremental/refresh runs.

        Returns (known_ids, stored_total_count, stored_cursor_incomplete,
        stored_pages).  When the conditions for an incremental scan are not
        met, returns (None, 0, False, []).
        """
        id_paths = spec.item_id_paths or ([spec.item_id_path] if spec.item_id_path else None)
        if (
            progress_cursor
            or mode not in ("incremental", "refresh")
            or spec.pagination_strategy == "none"
        ):
            return None, 0, False, []

        existing = await self._store.get_raw_payload(ep_name)
        if existing is None or id_paths is None:
            logger.info(
                "incremental_no_stored_data",
                extra={"uid": uid, "endpoint": ep_name},
            )
            return None, 0, False, []

        stored_pages = existing.get("pages", [])
        if not isinstance(stored_pages, list):
            stored_pages = []
        stored_total_count = _stored_total_count(stored_pages)
        stored_cursor_incomplete = _stored_cursor_incomplete(
            stored_pages, spec.pagination_strategy,
        )
        known_ids: set[str] = set()
        for stored_page in stored_pages:
            for item_id in _extract_item_ids_multi(stored_page, id_paths):
                known_ids.add(item_id)
        logger.info(
            "incremental_scan_started",
            extra={"uid": uid, "endpoint": ep_name, "known_id_count": len(known_ids)},
        )
        return known_ids, stored_total_count, stored_cursor_incomplete, stored_pages

    async def _prepare_request_state(
        self: Any,
        spec: EndpointSpec,
        ep_name: str,
        mode: str,
        known_ids: set[str] | None,
        progress: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[Any]]:
        """Derive initial request_params and stored_pages_base.

        Returns (request_params, stored_pages_base).
        """
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

        stored_pages_base: list[Any] = []
        if known_ids is None and spec.pagination_strategy != "none" and mode != "full":
            existing_payload = await self._store.get_raw_payload(ep_name)
            existing_pages = (existing_payload or {}).get("pages", [])
            if isinstance(existing_pages, list):
                stored_pages_base = list(existing_pages)

        return request_params, stored_pages_base

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

        # Load progress early. A non-empty cursor means a previous paginated
        # run committed payload+progress and then stopped before completion;
        # resume that cursor before doing normal known-id incremental scans.
        progress = await self._store.get_progress(ep_name)
        progress_cursor = progress.get("cursor") if progress is not None else None

        id_paths = spec.item_id_paths or ([spec.item_id_path] if spec.item_id_path else None)

        # -- incremental mode: build known_ids from stored data --
        (
            known_ids,
            stored_total_count,
            stored_cursor_incomplete,
            stored_pages,
        ) = await self._build_known_ids_for_incremental(
            uid, spec, ep_name, mode, progress_cursor,
        )

        initial_known_count = len(known_ids) if known_ids is not None else 0

        # -- prepare initial request params and stored pages base --
        request_params, stored_pages_base = await self._prepare_request_state(
            spec, ep_name, mode, known_ids, progress,
        )

        settings = self._settings
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_fetching_retry_delays()

        retry_state = FetchFailureState()

        # track pages fetched in THIS run (for incremental overwrite)
        pages_this_run: list[dict[str, Any]] = []
        boundary_hit = False  # set when all IDs on a page are known
        pagination_loop_detected = False
        seen_page_requests: set[tuple[tuple[str, str], ...]] = set()

        async def _emit_pagination_loop(params: dict[str, Any]) -> None:
            reporter = getattr(self, "_reporter", None)
            if reporter is None:
                return
            await reporter.emit(
                "fetch.endpoint.pagination_loop_detected",
                stage="fetching",
                level="WARNING",
                endpoint=ep_name,
                data={
                    "request_params": params,
                    "page_count": len(pages_this_run),
                },
            )

        async def _fetch_one_page(params: dict[str, Any]):
            await self._rl.acquire(spec.rate_limit_key)
            return await self._fetch_fn(
                uid, spec, credential, params,
                timeout=settings.bili_fetching_request_timeout,
            )

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            reporter = getattr(self, "_reporter", None)
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
                _log_unavailable(logger, namespace="fetch.endpoint", uid=uid,
                                 ep_name=ep_name, item_id=None, reason=str(exc))
                await _emit_unavailable(reporter, namespace="fetch.endpoint",
                                        ep_name=ep_name, item_id=None, exc=exc)
                return None

            if isinstance(exc, Http412Error):
                advice = await self._rl.record_412(spec.rate_limit_key)
                # rate-limit state is in-memory only (locked decision §11);
                # no persistence call here.
                retry_count = retry_state.bump()
                detail = {"retry_count": retry_count, "params": request_params}
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
                _log_retry_scheduled(logger, namespace="fetch.endpoint", uid=uid,
                                     ep_name=ep_name, item_id=None,
                                     retry=retry_state.count, wait_s=wait)
                await _emit_retry_scheduled(reporter, namespace="fetch.endpoint",
                                            ep_name=ep_name, item_id=None, exc=exc,
                                            retry=retry_state.count, delay_s=wait)
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
                _log_retry_scheduled(logger, namespace="fetch.endpoint", uid=uid,
                                     ep_name=ep_name, item_id=None,
                                     retry=retry_count, wait_s=outcome.delay_seconds)
                await _emit_retry_scheduled(reporter, namespace="fetch.endpoint",
                                            ep_name=ep_name, item_id=None, exc=exc,
                                            retry=retry_count, delay_s=outcome.delay_seconds)
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
                    await _emit_pagination_loop(request_params)
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
                await self._store.save_raw_page_and_progress(
                    ep_name,
                    "",
                    {"pages": [*stored_pages_base, *pages_this_run]},
                    {
                        "cursor": page.next_request,
                        "total": None,
                        "fetched": None,
                    },
                    fetched_at_ms=now_ms,
                )
                reporter = getattr(self, "_reporter", None)
                if reporter is not None:
                    await reporter.emit(
                        "fetch.endpoint.page_saved",
                        stage="fetching",
                        endpoint=ep_name,
                        data={
                            "page_count": len(stored_pages_base) + len(pages_this_run),
                            "next_request": page.next_request,
                            "is_last_page": page.is_last_page,
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
                    await _emit_pagination_loop(next_params)
                    break

            request_params = next_params

        # -- store results --
        if spec.pagination_strategy != "none":
            if known_ids is not None:
                # incremental: fresh pages first, but keep old unseen pages so
                # raw.db remains a durable input cache rather than a small
                # latest-window snapshot.
                raw_payload: dict[str, Any] = {
                    "pages": _merge_incremental_pages(
                        new_pages=pages_this_run,
                        stored_pages=stored_pages,
                        id_paths=id_paths,
                    ),
                }
            elif mode == "full":
                # full mode: overwrite entirely (do NOT accumulate)
                raw_payload = {"pages": pages_this_run}
            else:
                # resume / first-time incremental: keep pages committed before
                # this run, then append pages fetched in this run.
                raw_payload = {"pages": [*stored_pages_base, *pages_this_run]}
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
