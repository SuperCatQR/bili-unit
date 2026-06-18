"""Failure-state helpers for fetching runner modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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


__all__ = [
    "FetchFailureState",
    "classify_fetching_exception",
    "record_endpoint_failure",
    "record_unexpected_endpoint_failure",
]
