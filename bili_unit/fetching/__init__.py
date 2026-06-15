# bili_unit/fetching — common DTOs and exceptions

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._env import BiliSettings
    from .command import Command

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
# Assembly root — wires env → settings → rate limit → Command
# ---------------------------------------------------------------------------

async def assemble(settings: "BiliSettings | None" = None) -> "Command":
    """Read env, init HTTP backend, wire dependencies, return a Command.

    Phase 3 contract: returns a single ``Command``. The store layer is now
    request-scoped — Command opens its own ``UidContext`` + ``FetchingStore``
    inside each ``fetch_uid`` call.

    Args:
        settings: pre-built ``BiliSettings`` to use. ``None`` (default) lazy-loads
            from .env via :func:`bili_unit._env.get_settings` — keeps the historical
            CLI behaviour intact.
    """
    from .._env import get_settings
    from ._bilibili_adapter import init_http_backend
    from .command import Command
    from .rate_limit import RateLimitController

    s = settings if settings is not None else get_settings()
    init_http_backend(s.bili_fetching_http_backend, s.bili_fetching_impersonate)

    rl = RateLimitController(
        global_qps=s.bili_fetching_global_qps,
        endpoint_qps=s.bili_fetching_endpoint_qps,
        video_detail_qps=s.bili_fetching_video_detail_qps,
        recovery_cooldown=s.bili_fetching_recovery_cooldown,
    )
    stale_ms = int(s.bili_fetching_stale_running_threshold_seconds * 1000)
    return Command(s, rl, stale_running_threshold_ms=stale_ms)
