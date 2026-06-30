# Worker-path item fan-out + §8 zero-import guard.
#
# Added for CHO-98 review blocker #1: the worker item path used to early-return
# before RetryDriver, bypassing retry / error-recording / three-state
# classification. These tests pin the worker item path to the same retry +
# PERMANENT/UNAVAILABLE/FAILED behaviour as the in-process path, and guard the
# §8 "main process never imports bilibili_api" red line against silent regressions.

from __future__ import annotations

import sys
from pathlib import Path

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching._error_pack import ErrorPack
from bili_unit.fetching._store import FetchingStore
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.runner import Runner
from bili_unit.tests.fake_worker import FakeWorker


def _settings(tmp_path: Path) -> BiliSettings:
    settings = BiliSettings(bili_db_dir=str(tmp_path))
    # Fail fast on retryable errors instead of sleeping.
    settings.bili_fetching_max_retries = 1
    settings.bili_fetching_recovery_cooldown = 0
    settings.bili_fetching_request_timeout = 5.0
    return settings


def _fast_rate_limit() -> RateLimitController:
    return RateLimitController(
        global_qps=1000.0,
        endpoint_qps=1000.0,
        video_detail_qps=1000.0,
        pause_seconds=0,
    )


async def _open_store(tmp_path: Path, uid: int) -> tuple[UidContext, FetchingStore]:
    ctx = UidContext(uid=uid, root=tmp_path)
    await ctx.open()
    return ctx, FetchingStore(ctx)


async def _seed_videos_payload(store: FetchingStore, bvids: list[str]) -> None:
    """Seed the videos endpoint so video_detail item fan-out has bvids to fetch."""
    await store.init_task(["videos", "video_detail"])
    await store.save_raw_payload("videos", "", {
        "pages": [
            {
                "list": {"vlist": [{"bvid": bv} for bv in bvids]},
                "page": {"count": len(bvids)},
            },
        ],
    })
    await store.update_endpoint_state("videos", status=EndpointStatus.SUCCESS.value)
    await store.update_task_status(TaskStatus.SUCCESS.value)


def _runner(store: FetchingStore, settings: BiliSettings, *, worker: FakeWorker) -> Runner:
    return Runner(store, _fast_rate_limit(), settings, worker=worker)


def _started_worker() -> FakeWorker:
    """A FakeWorker configured as a started worker with a credential ref."""
    fake = FakeWorker()
    fake._started = True
    fake.responses["credential_open"] = {"credential_ref": "cred-1"}
    return fake


# ======================================================================
# §8 red line — main process must not import bilibili_api
# ======================================================================

def test_main_process_zero_bilibili_api_import():
    """Importing the fetching entry surface must NOT pull bilibili_api in.

    This is the arm's-length / non-GPL contract (docs/ipc-contract-f2.md §8).
    CHO-99 achieved 87→0; this guard keeps it from silently regressing.
    """
    # Drop any already-imported bilibili_api so the assertion measures the
    # import side-effect of fetching.command, not a prior test's residue.
    for mod in [m for m in list(sys.modules) if m.startswith("bilibili_api")]:
        del sys.modules[mod]

    import bili_unit.fetching.command  # noqa: F401

    leaked = [m for m in sys.modules if m.startswith("bilibili_api")]
    assert not leaked, f"main process leaked bilibili_api imports: {leaked[:5]}..."


# ======================================================================
# Worker item path — three-state classification parity with in-process path
# ======================================================================

async def test_worker_item_success_unwraps_envelope(tmp_path: Path):
    """Worker item fetch unwraps the {raw_payload} envelope and stores the data."""
    ctx, store = await _open_store(tmp_path, 300)
    try:
        await _seed_videos_payload(store, ["BV1", "BV2"])

        worker = _started_worker()

        async def _fetch_item(item_id, endpoint, cred_ref, extra, timeout=None):
            # Worker returns an envelope; runner must unwrap before storing.
            return {"raw_payload": {"info": {"bvid": item_id}, "tags": []}}

        worker.fetch_item = _fetch_item  # type: ignore[method-assign]

        result = await _runner(store, _settings(tmp_path), worker=worker).run_or_resume(
            300, endpoints=["video_detail"], mode="incremental",
        )

        assert result.status == TaskStatus.SUCCESS
        assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS
        items = await store.list_completed_items("video_detail")
        assert items == ["BV1", "BV2"]
        # Stored structure is the data, not the envelope.
        payload = await store.get_raw_payload("video_detail", "BV1")
        assert payload == {"info": {"bvid": "BV1"}, "tags": []}
    finally:
        await ctx.close()


async def test_worker_item_unavailable_business_code_skips_item(tmp_path: Path):
    """A permanent-unavailable business error skips only that item.

    Regression guard for blocker #1: the old worker early-return let a single
    item's ResourceUnavailableError (e.g. private video 53013/88214) fail the
    WHOLE endpoint as FAILED_PERMANENT. Correct behaviour mirrors the in-process
    path — the offending item is marked unavailable, the endpoint still succeeds.
    """
    ctx, store = await _open_store(tmp_path, 301)
    try:
        await _seed_videos_payload(store, ["BV_OK", "BV_PRIVATE"])

        worker = _started_worker()

        async def _fetch_item(item_id, endpoint, cred_ref, extra, timeout=None):
            if item_id == "BV_PRIVATE":
                # Worker reconstructs business errors as FetchingError subclasses
                # via the error pack; FakeWorker._dispatch raises the same way.
                raise fetching_exc_from_pack(ErrorPack(
                    type="ResourceUnavailableError",
                    classification="permanent",
                    code=53013,
                    message="video_detail[BV_PRIVATE]: code=53013",
                    retryable_hint=False,
                ))
            return {"raw_payload": {"info": {"bvid": item_id}, "tags": []}}

        worker.fetch_item = _fetch_item  # type: ignore[method-assign]

        result = await _runner(store, _settings(tmp_path), worker=worker).run_or_resume(
            301, endpoints=["video_detail"], mode="incremental",
        )

        # The endpoint succeeds overall; only the private item is unavailable.
        assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS
        done = await store.list_completed_items("video_detail")
        assert "BV_OK" in done
        assert "BV_PRIVATE" not in done
        # Error recorded against the item, not a wholesale endpoint failure.
        assert await store.get_raw_payload("video_detail", "BV_PRIVATE") is None
    finally:
        await ctx.close()


async def test_worker_item_transient_error_is_retried(tmp_path: Path):
    """A retryable error (Http412Error) is retried, not immediately permanent.

    Regression guard for blocker #1: the old worker early-return let transient
    errors escape RetryDriver, so 412 never retried. Now it flows through
    RetryDriver like the in-process path.
    """
    ctx, store = await _open_store(tmp_path, 302)
    try:
        await _seed_videos_payload(store, ["BV1"])

        worker = _started_worker()
        attempts = {"n": 0}

        async def _fetch_item(item_id, endpoint, cred_ref, extra, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise fetching_exc_from_pack(ErrorPack(
                    type="Http412Error",
                    classification="retryable",
                    code=412,
                    message="video_detail[BV1]: 412",
                    retryable_hint=True,
                ))
            return {"raw_payload": {"info": {"bvid": item_id}, "tags": []}}

        worker.fetch_item = _fetch_item  # type: ignore[method-assign]

        result = await _runner(store, _settings(tmp_path), worker=worker).run_or_resume(
            302, endpoints=["video_detail"], mode="incremental",
        )

        # Retried once (max_retries=1 → 2 attempts), then succeeded.
        assert attempts["n"] == 2
        assert result.endpoints.get("video_detail") == EndpointStatus.SUCCESS
        assert await store.list_completed_items("video_detail") == ["BV1"]
    finally:
        await ctx.close()


def fetching_exc_from_pack(pack: ErrorPack):
    """Reconstruct the fetching exception a real WorkerClient would raise."""
    from bili_unit.fetching._error_pack import fetching_exception_from_pack
    return fetching_exception_from_pack(pack)
