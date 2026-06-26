# tests for two fetch-stalling bugs (Phase 6 rewrite for SQLite stack).
"""Two bugs that left endpoints silently stuck in RUNNING:

1. ``media_list`` enum-in-params_strategy — the catalog used to embed
   ``user.MedialistOrder.PUBDATE`` (an enum) directly in ``params_strategy``.
   The runner persists ``params_strategy`` as JSON for progress/resume — but
   the enum is not JSON-serialisable, so the page-save step crashed with
   ``TypeError: Object of type MedialistOrder is not JSON serializable``.
   The exception escaped the retry-driver path entirely and got swallowed by
   the progress-aware gather layer, leaving ``media_list`` indefinitely
   RUNNING with no error record and no terminal state.

2. ``_run_endpoint`` silent-RUNNING leak — any exception escaping the
   endpoint runner's main body (storage failure, programmer error, etc.)
   would bubble through the gather shim and silently strand the endpoint.
   The runner now wraps its body so unexpected exceptions always produce a
   FAILED_PERMANENT entry plus an error record.

Phase 6 note: the static media_list checks are stage-agnostic and survive
unchanged. The runner safety-net check is rewritten against the SQLite
FetchingStore — instead of asserting on a fake KV store, we read back the
endpoint state row and stage_error rows directly.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bilibili_api import user

from bili_unit._db import UidContext
from bili_unit.fetching import EndpointStatus
from bili_unit.fetching._bilibili_adapter import fetch_user_media_list
from bili_unit.fetching._endpoint_catalog import get_endpoint
from bili_unit.fetching._endpoint_spec import EndpointSpec
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.runner._endpoint import _EndpointMixin

# -- 1. media_list params_strategy must be JSON-safe --------------------------


def test_media_list_params_strategy_is_json_serialisable():
    """The runner persists request_params (a copy of params_strategy) into
    progress as JSON. Embedding a Python enum here crashes the page-save
    silently and strands the endpoint at RUNNING.
    """
    spec = get_endpoint("media_list")
    assert spec is not None
    json.dumps(spec.params_strategy, ensure_ascii=False)


def test_media_list_sort_field_is_int():
    """``params_strategy['sort_field']`` must be the int form of the enum so
    it survives the JSON round-trip; the wrapper callable re-casts it back
    to the enum before invoking the SDK.
    """
    spec = get_endpoint("media_list")
    assert spec is not None
    sort_field = spec.params_strategy["sort_field"]
    assert isinstance(sort_field, int) and not isinstance(sort_field, bool)
    assert user.MedialistOrder(sort_field) == user.MedialistOrder.PUBDATE


def test_fetch_user_media_list_accepts_cred_keyword():
    """Mirror of the channels-keyword check: the unified call site uses
    ``cred=...``, so the function must accept that exact name.
    """
    sig = inspect.signature(fetch_user_media_list)
    params = sig.parameters
    assert "cred" in params
    assert params["cred"].default is None


def test_fetch_user_media_list_accepts_int_sort_field():
    """The wrapper must accept an int (because the catalog stores it as int
    for JSON safety) and re-cast it to the enum the SDK requires.
    """
    sig = inspect.signature(fetch_user_media_list)
    sort_param = sig.parameters.get("sort_field")
    assert sort_param is not None
    annotation = str(sort_param.annotation)
    assert "int" in annotation, (
        f"sort_field must accept int (catalog persists int for JSON safety); got annotation: {annotation}"
    )


# -- 2. runner silent-RUNNING safety net --------------------------------------


class _RunnerHarness(_EndpointMixin):
    """Minimal subclass to drive ``_run_endpoint`` standalone.

    Wraps a real FetchingStore (so the safety-net's assertions land on the
    actual SQLite tables we ship in production) plus stub rate-limit / fetch
    deps. The MRO trick mirrors how the production Runner composes the mixin.
    """

    def __init__(self, store: FetchingStore) -> None:
        self._store = store
        self._rl = MagicMock()
        self._rl.acquire = AsyncMock(return_value=None)
        self._settings = MagicMock()
        self._settings.bili_fetching_max_retries = 3
        self._settings.get_fetching_retry_delays = MagicMock(return_value=[0.0, 0.0, 0.0])
        self._settings.bili_fetching_request_timeout = 10.0
        self._fetch_fn = AsyncMock()


async def test_run_endpoint_catches_unexpected_exception_into_permanent(tmp_path: Path):
    """If the endpoint body raises something unexpected (e.g. JSON encoding
    blew up writing progress), the wrapper must convert it into a
    FAILED_PERMANENT terminal state plus a recorded error. Otherwise the
    endpoint stays in RUNNING forever and no failure surfaces.
    """
    ctx = UidContext(uid=42, root=tmp_path)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        await store.init_task(["media_list"])

        harness = _RunnerHarness(store)

        # Bypass the inner body entirely with a coroutine that raises a
        # non-FetchingError exception (TypeError mirrors the real-world
        # JSON-serialisation crash).
        async def _boom(*_a: Any, **_kw: Any) -> None:
            raise TypeError("Object of type MedialistOrder is not JSON serializable")

        harness._run_endpoint_inner = _boom  # type: ignore[assignment]

        spec = EndpointSpec(name="media_list", callable=lambda *_a, **_kw: None)
        await harness._run_endpoint(
            uid=42,
            spec=spec,
            ep_name="media_list",
            credential=None,
        )

        # The wrapper must record an error.
        errors = await store.list_errors(endpoint="media_list")
        assert errors, "wrapper must record the swallowed exception"
        last = errors[0]
        assert last["endpoint"] == "media_list"
        assert last["retryable"] is False
        assert "MedialistOrder" in last["message"]

        # The endpoint state row must be in a terminal FAILED_PERMANENT state
        # — anything else leaves it stranded mid-run.
        state = await store.get_endpoint_state("media_list")
        assert state is not None
        assert state["status"] == EndpointStatus.FAILED_PERMANENT.value
        assert state["last_error_id"] == last["id"]
    finally:
        await ctx.close()


def test_run_endpoint_inner_exists_and_remains_callable():
    """Sanity: refactor must keep the inner method in place so the wrapper
    has something to delegate to. Catches accidental rename / removal.
    """
    assert hasattr(_EndpointMixin, "_run_endpoint_inner")
    assert callable(_EndpointMixin._run_endpoint_inner)
