# runner._endpoint — single uid-level endpoint execution logic.

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

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
from .._endpoint_spec import EndpointSpec
from ..keys import _fetch_key, _progress_key
from ._item_ids import _extract_item_ids_multi

if TYPE_CHECKING:
    from ..._env import BiliSettings

logger = logging.getLogger("bili.fetching.runner")


def _classify_endpoint_exc(exc: Exception) -> RetryClassification:
    """Map fetching-layer exceptions to RetryDriver classifications.

    AuthError and ResourceUnavailableError are immediately permanent — no
    retries.  Other FetchingError subclasses (Http412Error, RequestError,
    Http5xxError) are retryable.  Anything else (logic bugs, serialisation
    errors) is treated as permanent so it surfaces fast rather than burning
    the retry budget on something retries cannot fix.
    """
    if isinstance(exc, (AuthError, ResourceUnavailableError)):
        return RetryClassification.PERMANENT
    if isinstance(exc, FetchingError):
        return RetryClassification.RETRYABLE
    return RetryClassification.PERMANENT


class _EndpointMixin:
    """Mixin providing ``_run_endpoint`` for :class:`Runner`.

    Accesses Runner state (``self._data``, ``self._error``, ``self._rl``)
    and helper methods (``_load_progress``, ``_update_endpoint_status``)
    via the combined MRO at runtime.
    """

    _data: Any
    _error: Any
    _fetch_fn: Any
    _rl: Any
    _settings: BiliSettings

    async def _load_progress(self, uid: int, endpoint: str) -> dict | None: ...  # pragma: no cover
    async def _update_endpoint_status(self, uid, ep_name, status, **kw) -> None: ...  # pragma: no cover

    # -- single endpoint ---------------------------------------------------

    async def _run_endpoint(
        self: Any,
        uid: int,
        spec: EndpointSpec,
        ep_name: str,
        credential: Any,
        mode: str = "incremental",
    ) -> None:
        # init
        await self._update_endpoint_status(uid, ep_name, EndpointStatus.RUNNING, retry_count=0)

        # -- incremental mode: build known_ids from stored data --
        known_ids: set[str] | None = None
        id_paths = spec.item_id_paths or ([spec.item_id_path] if spec.item_id_path else None)
        if mode in ("incremental", "refresh") and spec.pagination_strategy != "none":
            existing = await self._data.get(_fetch_key(uid, ep_name))
            if existing is not None and id_paths is not None:
                stored_pages = existing.get("raw_payload", {}).get("pages", [])
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
        progress = await self._load_progress(uid, ep_name)
        request_params = spec.params_strategy.copy()
        if known_ids is not None:
            # incremental mode: always start from page 1, ignore stored progress
            request_params = spec.params_strategy.copy()
        elif progress is not None:
            nr = progress.get("next_request")
            if nr:
                request_params = nr

        settings = self._settings
        max_retries = settings.bili_fetching_max_retries
        retry_delays = settings.get_fetching_retry_delays()

        # ``retry_count`` here is observable to the test harness (via the task
        # entry's retry_count field) and represents the number of failed
        # attempts so far.  Driven in the on_attempt_failed callback.
        retry_state = {"count": 0, "last_error_id": None}

        # track pages fetched in THIS run (for incremental overwrite)
        pages_this_run: list[dict[str, Any]] = []
        boundary_hit = False  # set when all IDs on a page are known

        async def _fetch_one_page(params: dict[str, Any]):
            await self._rl.acquire(spec.rate_limit_key)
            return await self._fetch_fn(
                uid, spec, credential, params,
                timeout=settings.bili_fetching_request_timeout,
            )

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            # Driver guarantees: PERMANENT → will_retry=False; RETRYABLE →
            # will_retry depends on attempt budget.  Caller decides what to
            # write to the store and (for 412) overrides the sleep duration.
            if isinstance(exc, AuthError):
                # Permanent — no retry.  Write FAILED_PERMANENT and let the
                # caller swallow the raise via try/except.
                await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable=False,
                )
                await self._update_endpoint_status(
                    uid, ep_name, EndpointStatus.FAILED_PERMANENT,
                    retry_count=retry_state["count"],
                )
                return None

            if isinstance(exc, ResourceUnavailableError):
                err_id = await self._error.record(
                    exc, uid=uid, endpoint=ep_name, retryable=False,
                )
                await self._update_endpoint_status(
                    uid, ep_name, EndpointStatus.FAILED_PERMANENT,
                    retry_count=retry_state["count"], last_error_id=err_id,
                )
                logger.info(
                    "endpoint_unavailable",
                    extra={"uid": uid, "endpoint": ep_name, "reason": str(exc)},
                )
                return None

            if isinstance(exc, Http412Error):
                advice = await self._rl.record_412(spec.rate_limit_key)
                # persist rate-limit state
                await self._data.put("rate_limit:global", self._rl.to_state())
                await self._data.put(
                    f"rate_limit:{spec.rate_limit_key}",
                    self._rl.to_state(endpoint=spec.rate_limit_key),
                )
                retry_state["count"] += 1
                detail = {
                    "retry_count": retry_state["count"],
                    "params": request_params,
                }
                err_id = await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
                    retryable=outcome.will_retry,
                    detail=detail,
                )
                retry_state["last_error_id"] = err_id
                await self._update_endpoint_status(
                    uid, ep_name, EndpointStatus.FAILED_RETRYABLE,
                    retry_count=retry_state["count"], last_error_id=err_id,
                )
                if not outcome.will_retry:
                    # Exhausted — final state is FAILED_EXHAUSTED.
                    await self._update_endpoint_status(
                        uid, ep_name, EndpointStatus.FAILED_EXHAUSTED,
                        retry_count=retry_state["count"], last_error_id=err_id,
                    )
                    return None
                wait = max(advice.get("wait_seconds", 0), outcome.delay_seconds)
                logger.info(
                    "retry_scheduled",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "wait_s": wait, "retry": retry_state["count"],
                    },
                )
                return wait

            if isinstance(exc, FetchingError):
                retry_state["count"] += 1
                err_id = await self._error.record(
                    exc, uid=uid, endpoint=ep_name,
                    retryable=outcome.will_retry,
                )
                retry_state["last_error_id"] = err_id
                await self._update_endpoint_status(
                    uid, ep_name, EndpointStatus.FAILED_RETRYABLE,
                    retry_count=retry_state["count"], last_error_id=err_id,
                )
                if not outcome.will_retry:
                    await self._update_endpoint_status(
                        uid, ep_name, EndpointStatus.FAILED_EXHAUSTED,
                        retry_count=retry_state["count"], last_error_id=err_id,
                    )
                    return None
                logger.info(
                    "retry_scheduled",
                    extra={
                        "uid": uid, "endpoint": ep_name,
                        "wait_s": outcome.delay_seconds,
                        "retry": retry_state["count"],
                    },
                )
                return None

            # Unexpected non-fetching error — wrap and treat as permanent.
            wrapped = FetchingError(f"unexpected: {type(exc).__name__}: {exc}")
            err_id = await self._error.record(
                wrapped, uid=uid, endpoint=ep_name, retryable=False,
            )
            await self._update_endpoint_status(
                uid, ep_name, EndpointStatus.FAILED_PERMANENT,
                retry_count=retry_state["count"], last_error_id=err_id,
            )
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=_classify_endpoint_exc,
        )
        driver = RetryDriver(policy)

        while True:
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
                _prog = {
                    "mode": spec.pagination_strategy,
                    "next_request": page.next_request,
                    "last_completed_request": request_params,
                    "done": page.is_last_page,
                    "updated_at": now_ms,
                }
                await self._data.put(_progress_key(uid, ep_name), _prog)

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
                    # all IDs on this page are known — boundary hit
                    boundary_hit = True
                    logger.info(
                        "incremental_boundary_hit",
                        extra={"uid": uid, "endpoint": ep_name},
                    )
                    # fetch one more safety page if not already at last page
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
                                extra={"uid": uid, "endpoint": ep_name, "error": str(exc)},
                            )
                    # stop after safety page
                    break

                # add new IDs to known set
                known_ids.update(str(i) for i in new_ids)

            # determine if we should stop (non-incremental or incremental with new content)
            if page.is_last_page or spec.pagination_strategy == "none":
                break

            request_params = page.next_request or request_params

        # -- store results --
        if spec.pagination_strategy != "none":
            if known_ids is not None:
                # incremental: overwrite with pages from this run only
                raw_payload = {"pages": pages_this_run}
            elif mode == "full":
                # full mode: overwrite entirely (do NOT accumulate)
                raw_payload = {"pages": pages_this_run}
            else:
                # incremental first run (no stored data): accumulate on existing
                existing = await self._data.get(_fetch_key(uid, ep_name))
                pages = (existing or {}).get("raw_payload", {}).get("pages", [])
                pages.extend(pages_this_run)
                raw_payload = {"pages": pages}
        else:
            raw_payload = pages_this_run[0] if pages_this_run else page.raw_payload

        fetch_val = {
            "uid": uid,
            "endpoint": ep_name,
            "status": "SUCCESS",
            "raw_payload": raw_payload,
            "fetched_at": now_ms,
            "updated_at": now_ms,
        }

        # build next progress
        next_progress = None
        if spec.pagination_strategy != "none":
            if page.is_last_page or boundary_hit:
                next_progress = {
                    "mode": spec.pagination_strategy,
                    "next_request": None,
                    "last_completed_request": request_params,
                    "done": True,
                    "updated_at": now_ms,
                }
            else:
                next_progress = {
                    "mode": spec.pagination_strategy,
                    "next_request": page.next_request,
                    "last_completed_request": request_params,
                    "done": False,
                    "updated_at": now_ms,
                }

        # transactional write
        if next_progress is not None:
            await self._data.write_fetch_page_and_progress(
                _fetch_key(uid, ep_name),
                fetch_val,
                _progress_key(uid, ep_name),
                next_progress,
            )
        else:
            await self._data.put(_fetch_key(uid, ep_name), fetch_val)

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

        await self._update_endpoint_status(
            uid, ep_name, EndpointStatus.SUCCESS,
            retry_count=retry_state["count"],
        )
