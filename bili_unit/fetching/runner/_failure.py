"""Failure-state helpers for fetching runner modules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..._retry import RetryClassification
from .. import (
    AuthError,
    EndpointStatus,
    FetchingError,
    InvalidRequestError,
    ResourceUnavailableError,
)

if TYPE_CHECKING:
    from .._store import FetchingStore


@dataclass
class FetchFailureState:
    """Mutable retry state for one endpoint run."""

    count: int = 0
    last_error_id: int | None = None

    def bump(self) -> int:
        self.count += 1
        return self.count


def classify_fetching_exception(exc: Exception) -> RetryClassification:
    """Classify fetching exceptions for RetryDriver."""
    if isinstance(exc, (AuthError, InvalidRequestError, ResourceUnavailableError)):
        return RetryClassification.PERMANENT
    if isinstance(exc, FetchingError):
        return RetryClassification.RETRYABLE
    return RetryClassification.PERMANENT


async def record_endpoint_failure(
    store: FetchingStore,
    *,
    endpoint: str,
    status: EndpointStatus,
    state: FetchFailureState,
    exc: Exception,
    retryable: bool,
    detail: dict[str, Any] | None = None,
) -> int:
    """Record a fetching error and update endpoint state in one call."""
    err_id = await store.record_error(
        endpoint=endpoint,
        error_type=type(exc).__name__,
        message=str(exc),
        retryable=retryable,
        detail=detail,
    )
    state.last_error_id = err_id
    await store.update_endpoint_state(
        endpoint,
        status=status.value,
        retry_count=state.count,
        last_error_id=err_id,
    )
    return err_id


async def record_unexpected_endpoint_failure(
    store: FetchingStore,
    *,
    endpoint: str,
    state: FetchFailureState,
    exc: Exception,
) -> int:
    """Record an unexpected exception as a permanent fetching failure."""
    err_id = await store.record_error(
        endpoint=endpoint,
        error_type="FetchingError",
        message=f"unexpected: {type(exc).__name__}: {exc}",
        retryable=False,
    )
    state.last_error_id = err_id
    await store.update_endpoint_state(
        endpoint,
        status=EndpointStatus.FAILED_PERMANENT.value,
        retry_count=state.count,
        last_error_id=err_id,
    )
    return err_id


async def _emit_retry_scheduled(
    reporter: Any | None,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    ep_name: str,
    item_id: str | None,
    exc: Exception,
    retry: int,
    delay_s: float,
) -> None:
    """Emit a retry_scheduled event for endpoint- or item-scope."""
    if reporter is None:
        return
    kw: dict[str, Any] = dict(
        stage="fetching",
        level="WARNING",
        endpoint=ep_name,
        message=str(exc),
        data={
            "retry": retry,
            "delay_s": delay_s,
            "error_type": type(exc).__name__,
        },
    )
    if namespace == "fetch.item":
        kw["item_type"] = ep_name
        kw["item_id"] = item_id
    await reporter.emit(f"{namespace}.retry_scheduled", **kw)


async def _emit_failed(
    reporter: Any | None,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    ep_name: str,
    item_id: str | None,
    exc: Exception,
    retry: int,
    extra_data: dict[str, Any] | None = None,
) -> None:
    """Emit a .failed event.  No-op for endpoint namespace (no such event there).

    The endpoint-level retry callback in ``_endpoint.py`` deliberately omits
    the ``fetch.endpoint.failed`` event — exhaustion is signalled by the
    final ``EndpointStatus.FAILED_EXHAUSTED`` state write, not an event.
    Item-level callbacks do emit ``fetch.item.failed``.
    """
    if reporter is None or namespace == "fetch.endpoint":
        return
    data: dict[str, Any] = {"retry": retry, "error_type": type(exc).__name__}
    if extra_data:
        data.update(extra_data)
    await reporter.emit(
        f"{namespace}.failed",
        stage="fetching",
        level="ERROR",
        endpoint=ep_name,
        item_type=ep_name,
        item_id=item_id,
        message=str(exc),
        data=data,
    )


async def _emit_unexpected_failed(
    reporter: Any | None,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    ep_name: str,
    item_id: str | None,
    exc: Exception,
) -> None:
    """Emit a .failed event for unexpected (non-fetching) errors.

    Same scope rule as :func:`_emit_failed` — only ``fetch.item`` emits;
    endpoint-level unexpected errors are recorded via the store but not
    surfaced as a discrete observability event.
    """
    if reporter is None or namespace == "fetch.endpoint":
        return
    await reporter.emit(
        f"{namespace}.failed",
        stage="fetching",
        level="ERROR",
        endpoint=ep_name,
        item_type=ep_name,
        item_id=item_id,
        message=str(exc),
        data={"error_type": type(exc).__name__},
    )


async def _emit_unavailable(
    reporter: Any | None,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    ep_name: str,
    item_id: str | None,
    exc: Exception,
) -> None:
    """Emit a .unavailable event."""
    if reporter is None:
        return
    kw: dict[str, Any] = dict(
        stage="fetching",
        level="WARNING",
        endpoint=ep_name,
        message=str(exc),
    )
    if namespace == "fetch.item":
        kw["item_type"] = ep_name
        kw["item_id"] = item_id
    await reporter.emit(f"{namespace}.unavailable", **kw)


def _log_retry_scheduled(
    logger: logging.Logger,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    uid: int,
    ep_name: str,
    item_id: str | None,
    retry: int,
    wait_s: float,
    reason: str | None = None,
) -> None:
    if namespace == "fetch.endpoint":
        extra: dict[str, Any] = {
            "uid": uid,
            "endpoint": ep_name,
            "wait_s": wait_s,
            "retry": retry,
        }
        logger.info("retry_scheduled", extra=extra)
    else:
        extra = {
            "uid": uid,
            "endpoint": ep_name,
            "item_id": item_id,
            "wait_s": wait_s,
            "retry": retry,
        }
        if reason is not None:
            extra["reason"] = reason
        logger.info("item_endpoint_retry", extra=extra)


def _log_exhausted(
    logger: logging.Logger,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    uid: int,
    ep_name: str,
    item_id: str | None,
    retry: int,
    reason: str | None = None,
) -> None:
    if namespace == "fetch.endpoint":
        # endpoint side does not log on exhaustion in this branch
        return
    extra: dict[str, Any] = {
        "uid": uid,
        "endpoint": ep_name,
        "item_id": item_id,
        "retry": retry,
    }
    if reason is not None:
        extra["reason"] = reason
    logger.warning("item_endpoint_item_exhausted", extra=extra)


def _log_unavailable(
    logger: logging.Logger,
    *,
    namespace: Literal["fetch.endpoint", "fetch.item"],
    uid: int,
    ep_name: str,
    item_id: str | None,
    reason: str,
) -> None:
    if namespace == "fetch.endpoint":
        logger.info(
            "endpoint_unavailable",
            extra={"uid": uid, "endpoint": ep_name, "reason": reason},
        )
    else:
        logger.info(
            "item_endpoint_item_unavailable",
            extra={"uid": uid, "endpoint": ep_name, "item_id": item_id, "reason": reason},
        )


__all__ = [
    "FetchFailureState",
    "classify_fetching_exception",
    "record_endpoint_failure",
    "record_unexpected_endpoint_failure",
    # emit helpers
    "_emit_retry_scheduled",
    "_emit_failed",
    "_emit_unexpected_failed",
    "_emit_unavailable",
    # log helpers
    "_log_retry_scheduled",
    "_log_exhausted",
    "_log_unavailable",
]
