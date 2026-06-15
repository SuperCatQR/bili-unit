# tests for two fetch-stalling bugs uncovered while running uid 3494380472109167:
# Run: uv run pytest bili_unit/tests/test_fetching_media_list_and_runner_safety.py -v
"""Two bugs that left endpoints silently stuck in RUNNING:

1. ``media_list`` enum-in-params_strategy — the catalog used to embed
   ``user.MedialistOrder.PUBDATE`` (an enum) directly in ``params_strategy``.
   The runner persists ``params_strategy`` as JSON for progress/resume — but
   the enum is not JSON-serialisable, so the page-save step crashed with
   ``TypeError: Object of type MedialistOrder is not JSON serializable``.
   The exception escaped the retry-driver path entirely and got swallowed
   by ``_gather_with_progress``, leaving ``media_list`` indefinitely RUNNING
   with no error record and no terminal state.

2. ``_run_endpoint`` silent-RUNNING leak — any exception escaping the
   endpoint runner's main body (storage failure, programmer error, etc.)
   would bubble through the gather shim and silently strand the endpoint.
   The runner now wraps its body so unexpected exceptions always produce a
   FAILED_PERMANENT entry plus an error record.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bilibili_api import user

from bili_unit.fetching import EndpointStatus
from bili_unit.fetching._bilibili_adapter import fetch_user_media_list
from bili_unit.fetching._endpoint_catalog import get_endpoint
from bili_unit.fetching._endpoint_spec import EndpointSpec
from bili_unit.fetching.runner._endpoint import _EndpointMixin

# -- 1. media_list params_strategy must be JSON-safe --------------------------

def test_media_list_params_strategy_is_json_serialisable():
    """The runner persists request_params (a copy of params_strategy) into
    progress as JSON.  Embedding a Python enum here crashes the page-save
    silently and strands the endpoint at RUNNING."""
    spec = get_endpoint("media_list")
    assert spec is not None
    # Round-trip through JSON; this is exactly what the runner does at
    # ``_endpoint.py``::``self._data.put(_progress_key(...), _prog)``.
    json.dumps(spec.params_strategy, ensure_ascii=False)


def test_media_list_sort_field_is_int():
    """``params_strategy['sort_field']`` must be the int form of the enum so
    it survives the JSON round-trip; the wrapper callable re-casts it back
    to the enum before invoking the SDK."""
    spec = get_endpoint("media_list")
    assert spec is not None
    sort_field = spec.params_strategy["sort_field"]
    assert isinstance(sort_field, int) and not isinstance(sort_field, bool)
    # And it must round-trip to the right enum.
    assert user.MedialistOrder(sort_field) == user.MedialistOrder.PUBDATE


def test_fetch_user_media_list_accepts_cred_keyword():
    """Mirror of the channels-keyword check: the unified call site uses
    ``cred=...``, so the function must accept that exact name."""
    sig = inspect.signature(fetch_user_media_list)
    params = sig.parameters
    assert "cred" in params
    assert params["cred"].default is None


def test_fetch_user_media_list_accepts_int_sort_field():
    """The wrapper must accept an int (because the catalog stores it as int
    for JSON safety) and re-cast it to the enum the SDK requires."""
    sig = inspect.signature(fetch_user_media_list)
    sort_param = sig.parameters.get("sort_field")
    assert sort_param is not None
    # Default should be a usable enum value (so direct calls keep working).
    # Annotation should permit int.
    annotation = str(sort_param.annotation)
    assert "int" in annotation, (
        f"sort_field must accept int (catalog persists int for JSON safety); "
        f"got annotation: {annotation}"
    )


# -- 2. runner silent-RUNNING safety net --------------------------------------

class _FakeStore:
    """Async key-value store stub backed by a dict."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def put(self, key: str, value: Any) -> None:
        self.data[key] = value

    async def write_fetch_page_and_progress(
        self, fetch_key: str, fetch_val: Any, progress_key: str, progress_val: Any,
    ) -> None:
        self.data[fetch_key] = fetch_val
        self.data[progress_key] = progress_val


class _FakeErrorStore:
    """Async error recorder stub."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._next_id = 1

    async def record(
        self, exc: Exception, *, uid: int, endpoint: str | None = None,
        retryable: bool = True, detail: Any = None,
    ) -> int:
        rid = self._next_id
        self._next_id += 1
        self.records.append({
            "id": rid, "uid": uid, "endpoint": endpoint,
            "type": type(exc).__name__, "message": str(exc),
            "retryable": retryable,
        })
        return rid


class _RunnerHarness(_EndpointMixin):
    """Minimal subclass to drive ``_run_endpoint`` standalone."""

    def __init__(self) -> None:
        self._data = _FakeStore()
        self._error = _FakeErrorStore()
        self._rl = MagicMock()
        self._rl.acquire = AsyncMock(return_value=None)
        self._settings = MagicMock()
        self._settings.bili_fetching_max_retries = 3
        self._settings.get_fetching_retry_delays = MagicMock(return_value=[0.0, 0.0, 0.0])
        self._settings.bili_fetching_request_timeout = 10.0
        self._fetch_fn = AsyncMock()
        self._status_writes: list[tuple[str, EndpointStatus, dict[str, Any]]] = []

    async def _load_progress(self, uid: int, endpoint: str) -> Any:
        return None

    async def _update_endpoint_status(
        self, uid: int, ep_name: str, status: EndpointStatus, **kw: Any,
    ) -> None:
        self._status_writes.append((ep_name, status, kw))


@pytest.mark.asyncio
async def test_run_endpoint_catches_unexpected_exception_into_permanent():
    """If the endpoint body raises something unexpected (e.g. JSON encoding
    blew up writing progress), the wrapper must convert it into a
    FAILED_PERMANENT terminal state plus a recorded error.  Otherwise the
    endpoint stays in RUNNING forever and no failure surfaces."""
    harness = _RunnerHarness()

    # Bypass the inner body entirely with a coroutine that raises a
    # non-FetchingError exception (TypeError mirrors the real-world
    # JSON-serialisation crash).
    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise TypeError("Object of type MedialistOrder is not JSON serializable")

    harness._run_endpoint_inner = _boom  # type: ignore[assignment]

    spec = EndpointSpec(name="media_list", callable=lambda *_a, **_kw: None)
    await harness._run_endpoint(uid=42, spec=spec, ep_name="media_list", credential=None)

    # The wrapper must record an error and write FAILED_PERMANENT.
    assert harness._error.records, "wrapper must record the swallowed exception"
    last = harness._error.records[-1]
    assert last["endpoint"] == "media_list"
    assert last["retryable"] is False
    assert "MedialistOrder" in last["message"]

    statuses = [s for _, s, _ in harness._status_writes]
    assert EndpointStatus.FAILED_PERMANENT in statuses, (
        "endpoint must end in FAILED_PERMANENT, never stranded at RUNNING"
    )
    # FAILED_PERMANENT must be the LAST status write — anything after would
    # leave a non-terminal state on disk.
    assert statuses[-1] == EndpointStatus.FAILED_PERMANENT


@pytest.mark.asyncio
async def test_run_endpoint_inner_exists_and_remains_callable():
    """Sanity: refactor must keep the inner method in place so the wrapper
    has something to delegate to.  Catches accidental rename / removal."""
    assert hasattr(_EndpointMixin, "_run_endpoint_inner")
    assert callable(_EndpointMixin._run_endpoint_inner)
