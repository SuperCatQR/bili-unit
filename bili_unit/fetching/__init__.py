# bili_unit/fetching — common DTOs and exceptions

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

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


class InvalidRequestError(FetchingError):
    """Non-retryable invalid SDK/request arguments."""


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
    run_id: str | None = None


@dataclass
class TaskResult:
    uid: int
    status: TaskStatus
    endpoints: dict[str, EndpointStatus] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Assembly root — wires env → settings → rate limit → Command
# ---------------------------------------------------------------------------

def _infer_page_pagination(
    spec: Any,
    raw_payload: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Fallback for older workers that return only ``{raw_payload}``.

    The preferred contract is worker-side ``is_last_page`` + ``next_request``.
    Until the Git dependency is advanced to a worker build with that envelope,
    infer page-strategy cursors from the same response metadata the worker will
    use, so real ``videos`` and other page endpoints do not stop after page 1.
    """
    from ._adapter_core import extract_list_items, extract_total_count, resolve_dot_path

    total_count = extract_total_count(raw_payload)
    page_info = raw_payload.get("page")
    if isinstance(page_info, dict):
        total_count = total_count or page_info.get("count", 0)
    if total_count == 0 and "totalSize" in raw_payload:
        total_count = raw_payload.get("totalSize", 0)
    if total_count == 0:
        items_lists_page = resolve_dot_path(raw_payload, "items_lists.page")
        if isinstance(items_lists_page, dict):
            total_count = items_lists_page.get("total", 0)
    if total_count == 0 and isinstance(raw_payload.get("count"), int):
        total_count = raw_payload["count"]
    if total_count == 0 and isinstance(raw_payload.get("total_count"), int):
        total_count = raw_payload["total_count"]

    items = extract_list_items(raw_payload, getattr(spec, "items_path", None))
    current_pn = request_params.get("pn", 1)
    ps = request_params.get("ps", 30)
    if not items or (total_count > 0 and current_pn * ps >= total_count):
        return True, None
    return False, {**request_params, "pn": current_pn + 1, "ps": ps}


async def assemble(
    settings: "BiliSettings | None" = None,
    *,
    use_worker: bool = False,
) -> "Command":
    """Read env, init HTTP backend, wire dependencies, return a Command.

    Phase 3 contract: returns a single ``Command``. The store layer is now
    request-scoped — Command opens its own ``UidContext`` + ``FetchingStore``
    inside each ``fetch_uid`` call.

    Args:
        settings: pre-built ``BiliSettings`` to use. ``None`` (default) lazy-loads
            from .env via :func:`bili_unit._env.get_settings` — keeps the historical
            CLI behaviour intact.
        use_worker: If True, spawn a bili-worker subprocess and route fetch_page /
            fetch_item / resolve_audio_url through IPC. The worker must be installed
            (``bili-worker`` console script). Default False keeps the in-process
            bilibili_api path for backward compatibility.
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

    worker = None
    fetch_fn = None
    if use_worker:
        from ._bilibili_adapter import FetchPageResult
        from .worker_client import WorkerClient
        worker = WorkerClient()
        await worker.start(
            http_backend=s.bili_fetching_http_backend,
            impersonate=s.bili_fetching_impersonate,
        )

        async def _worker_fetch_page(
            uid: int, spec, credential, params, timeout: float = 30.0,
        ) -> FetchPageResult:
            """Route fetch_endpoint calls through the worker subprocess."""
            data = await worker.fetch_page(
                uid, spec.name, worker.credential_ref, params, timeout=timeout,
            )
            raw_payload = data["raw_payload"]
            is_last_page = data.get("is_last_page")
            next_request = data.get("next_request")
            if (
                next_request is None
                and is_last_page in (None, False)
                and spec.pagination_strategy == "page"
                and isinstance(raw_payload, dict)
            ):
                is_last_page, next_request = _infer_page_pagination(spec, raw_payload, params)
            return FetchPageResult(
                uid=uid,
                endpoint=spec.name,
                raw_payload=raw_payload,
                is_last_page=bool(is_last_page),
                next_request=next_request,
            )
        fetch_fn = _worker_fetch_page

    cmd = Command(s, rl, stale_running_threshold_ms=stale_ms, fetch_fn=fetch_fn, worker=worker)
    return cmd
