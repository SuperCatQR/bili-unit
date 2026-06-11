# bili_unit/fetching — common DTOs and exceptions

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Task / Endpoint status enums (cf. fetching_engineering.md §12)
# ---------------------------------------------------------------------------

class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_EXHAUSTED = "FAILED_EXHAUSTED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class EndpointStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_ITEM = "PARTIAL_ITEM"          # item-level fan-out: some items succeeded, some failed
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_EXHAUSTED = "FAILED_EXHAUSTED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


# ---------------------------------------------------------------------------
# Exceptions (cf. fetching_design.md §10)
# ---------------------------------------------------------------------------

class FetchingError(Exception):
    """Base for all fetching-layer exceptions."""


class AuthError(FetchingError):
    """Authentication failure (missing / expired / rejected)."""


class RateLimitError(FetchingError):
    """Reserved for future use.

    Intended to be raised by rate_limit.acquire() when the caller must wait
    (e.g. QPS exhausted or pause active). Currently unused — 412 handling
    goes through Http412Error instead.  Do NOT remove; will be activated
    when explicit back-pressure signalling is needed beyond the current
    cooldown-based QPS recovery mechanism.
    """


class RequestError(FetchingError):
    """HTTP request-level failure."""


class Http412Error(RequestError):
    """Bilibili 412 — too many requests."""


class Http5xxError(RequestError):
    """Server-side error (5xx)."""


class ResourceUnavailableError(FetchingError):
    """Permanent business-level failure — the resource is not (and will not be) available.

    Raised when:
      * The B站 API returns a known terminal business code (e.g. 53013 "用户隐私设置未公开",
        88214 "up未开通充电") — retrying yields the same response.
      * Article body parsing fails because the page response carries no ``readInfo`` —
        the article has been taken down or the page shape changed; retries cannot help.

    Runner treats this as ``FAILED_PERMANENT`` (uid-level) or skips the item without
    retry (item-level fan-out).  Distinct from :class:`AuthError` so it does NOT abort
    a whole fan-out — only the single failing item / endpoint.
    """


class DataError(FetchingError):
    """Storage / serialisation failure."""


# ---------------------------------------------------------------------------
# Query DTOs (cf. fetching_engineering.md §11)
# ---------------------------------------------------------------------------

@dataclass
class EndpointDTO:
    uid: int
    endpoint: str
    status: EndpointStatus
    available: bool
    raw_payload: dict[str, Any] | None = None
    fetched_at: int | None = None
    progress: dict[str, Any] | None = None
    errors: list["ErrorDTO"] = field(default_factory=list)


@dataclass
class TaskDTO:
    uid: int
    status: TaskStatus
    endpoints: dict[str, EndpointDTO] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None


@dataclass
class ErrorDTO:
    id: int
    uid: int | None
    endpoint: str | None
    error_type: str
    message: str
    retryable: str  # "true" | "false" | "unknown"
    detail: dict[str, Any] | None = None
    timestamp: int | None = None


# ---------------------------------------------------------------------------
# Command / Runner result types
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    uid: int
    status: TaskStatus


@dataclass
class TaskResult:
    uid: int
    status: TaskStatus
    endpoints: dict[str, EndpointStatus] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Assembly root — wires env → stores → components → entry points
# ---------------------------------------------------------------------------

async def assemble() -> tuple:
    """Read env, open stores, wire dependencies, return (Command, Query, DataStore, ErrorStore).

    Caller is responsible for closing stores via ``await data.close()`` /
    ``await error.close()`` when done.
    """
    from .client import init_http_backend
    from .command import Command
    from .data import DataStore
    from .env import get_settings
    from .error import ErrorStore
    from .query import Query
    from .rate_limit import RateLimitController

    s = get_settings()
    init_http_backend(s.bili_fetching_http_backend, s.bili_fetching_impersonate)

    data = DataStore(s.bili_fetching_data_dir)
    error = ErrorStore(s.bili_fetching_error_dir)
    await data.open()
    await error.open()

    rl = RateLimitController(
        global_qps=s.bili_fetching_global_qps,
        endpoint_qps=s.bili_fetching_endpoint_qps,
        video_detail_qps=s.bili_fetching_video_detail_qps,
        recovery_cooldown=s.bili_fetching_recovery_cooldown,
    )
    cmd = Command(data, error, rl)
    qry = Query(data, error)
    return cmd, qry, data, error
