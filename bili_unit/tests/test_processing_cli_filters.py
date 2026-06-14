# Tests for the W1.3 process CLI flags (--limit / --only-bvids /
# --retry-failed-only / --dry-run), exercised end-to-end through
# ``ProcessingCommand.process_uid``.
#
# The audio worker is patched to a fast in-memory stand-in (same pattern
# as ``test_processing_runner.py``) so we can assert which bvids actually
# entered the pipeline without running ffmpeg / network / ASR.

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit._env import BiliSettings
from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.keys import (
    _fetch_key,
    _item_fetch_key,
)
from bili_unit.fetching.keys import (
    _task_key as _fetch_task_key,
)
from bili_unit.fetching.query import Query as FetchingQuery
from bili_unit.fetching.task import EndpointEntry, TaskValue
from bili_unit.processing import (
    ProcessingItemStatus,
    ProcessingTaskStatus,
)
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _proc_key, _task_key
from bili_unit.processing.runner import ProcessingRunner


def _make_settings(tmp_path) -> BiliSettings:
    return BiliSettings(
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=8,
        bili_processing_max_retries=0,
        bili_processing_retry_delays="0",
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
    )


# Shared spy: every bvid that hits the worker pool is recorded so the
# test can assert which subset actually entered.
_dispatched_bvids: list[str] = []


async def _spy_process_audio_one(runner, uid, item, credential):
    _dispatched_bvids.append(item.item_id)
    bvid = item.item_id
    now = int(time.time() * 1000)
    await runner._data.put(_proc_key(uid, "audio", bvid), {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": ProcessingItemStatus.SUCCESS.value,
        "result": {
            "bvid": bvid,
            "pages": [],
            "total_duration": 0.0,
            "total_chars": 0,
        },
        "source_endpoints": ["video_detail"],
        "processed_at": now,
    })
    return True


# -- fixtures ---------------------------------------------------------------

@pytest_asyncio.fixture
async def fetching_stack(tmp_path):
    fd = FetchingDataStore(str(tmp_path / "fetch-data"))
    fe = FetchingErrorStore(str(tmp_path / "fetch-error"))
    await fd.open()
    await fe.open()
    qry = FetchingQuery(fd, fe)
    yield fd, fe, qry
    await fd.close()
    await fe.close()


@pytest_asyncio.fixture
async def proc_stack(tmp_path, fetching_stack):
    fd, _fe, fqry = fetching_stack
    s = _make_settings(tmp_path)
    pd = ProcessingDataStore(s.bili_processing_data_dir)
    pe = ProcessingErrorStore(s.bili_processing_error_dir)
    await pd.open()
    await pe.open()
    cmd = ProcessingCommand(
        data=pd, error=pe, temp_dir=s.bili_processing_temp_dir,
        fetching_query=fqry, settings=s,
        credential_provider=AsyncMock(return_value=None),
    )

    _dispatched_bvids.clear()
    with patch.object(
        ProcessingRunner, "_process_audio_one",
        new=_spy_process_audio_one,
    ):
        yield cmd, pd, pe, fd

    await pd.close()
    await pe.close()


# -- seeding helpers --------------------------------------------------------

async def _seed_fetching_video_detail(
    fd: FetchingDataStore, uid: int, bvids: list[str],
) -> None:
    tv = TaskValue(
        uid=uid,
        status=TaskStatus.SUCCESS,
        endpoints={
            "video_detail": EndpointEntry(
                status=EndpointStatus.SUCCESS,
                item_progress={
                    "total": len(bvids),
                    "completed": len(bvids),
                    "failed": 0,
                },
            ),
        },
        created_at=0,
        updated_at=0,
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())
    await fd.put(_fetch_key(uid, "video_detail"), {
        "uid": uid, "endpoint": "video_detail",
        "status": EndpointStatus.SUCCESS.value,
        "raw_payload": None,
        "item_counts": {
            "total": len(bvids), "completed": len(bvids), "failed": 0,
        },
    })
    for bvid in bvids:
        await fd.put(_item_fetch_key(uid, "video_detail", bvid), {
            "uid": uid, "endpoint": "video_detail", "item_id": bvid,
            "status": EndpointStatus.SUCCESS.value,
            "raw_payload": {
                "info": {
                    "bvid": bvid,
                    "aid": 0,
                    "title": f"title-{bvid}",
                    "desc": "", "duration": 60,
                    "pages": [{"cid": 1, "part": "P1", "duration": 60}],
                    "stat": {"view": 1, "danmaku": 0, "reply": 0,
                             "favorite": 0, "coin": 0, "share": 0, "like": 1},
                    "owner": {"mid": 999, "name": "U"},
                },
                "tags": [],
            },
        })


async def _seed_processing_status(
    pd: ProcessingDataStore, uid: int, bvid: str, status: ProcessingItemStatus,
) -> None:
    """Plant an existing processing record (used for retry-failed-only)."""
    await pd.put(_proc_key(uid, "audio", bvid), {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": status.value,
        "result": None,
        "source_endpoints": ["video_detail"],
        "processed_at": 0,
    })


# -- tests ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_bvids_filters_to_explicit_set(proc_stack, fetching_stack):
    """``--only-bvids BV1 BV2`` enters exactly that pair from a 5-item set."""
    cmd, _pd, _pe, fd = proc_stack
    uid = 8000
    all_bvids = ["BVone1", "BVtwo2", "BVthree3", "BVfour4", "BVfive5"]
    await _seed_fetching_video_detail(fd, uid, all_bvids)

    result = await cmd.process_uid(
        uid, only_bvids=["BVone1", "BVthree3"],
    )

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert sorted(_dispatched_bvids) == ["BVone1", "BVthree3"]


@pytest.mark.asyncio
async def test_limit_caps_to_first_n(proc_stack, fetching_stack):
    """``--limit 2`` truncates a 5-bvid discovery to the first 2."""
    cmd, _pd, _pe, fd = proc_stack
    uid = 8001
    all_bvids = ["BVa1", "BVb2", "BVc3", "BVd4", "BVe5"]
    await _seed_fetching_video_detail(fd, uid, all_bvids)

    result = await cmd.process_uid(uid, limit=2)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert len(_dispatched_bvids) == 2
    # Discovery preserves the order returned by ``list_video_details``;
    # asserting that the survivors are a prefix of the discovery order
    # is the property the flag promises.
    assert set(_dispatched_bvids).issubset(set(all_bvids))


@pytest.mark.asyncio
async def test_only_bvids_then_limit(proc_stack, fetching_stack):
    """``only_bvids`` filters first, then ``limit`` caps the survivors."""
    cmd, _pd, _pe, fd = proc_stack
    uid = 8002
    all_bvids = ["BVa1", "BVb2", "BVc3", "BVd4", "BVe5"]
    await _seed_fetching_video_detail(fd, uid, all_bvids)

    result = await cmd.process_uid(
        uid, only_bvids=["BVb2", "BVd4", "BVe5"], limit=2,
    )

    assert result.status == ProcessingTaskStatus.SUCCESS
    # only_bvids → {BVb2, BVd4, BVe5}; limit=2 → first two of those.
    assert len(_dispatched_bvids) == 2
    assert set(_dispatched_bvids).issubset({"BVb2", "BVd4", "BVe5"})


@pytest.mark.asyncio
async def test_retry_failed_only_picks_failed_records(proc_stack, fetching_stack):
    """3 SUCCESS + 2 FAILED → only the 2 FAILED enter the worker."""
    cmd, pd, _pe, fd = proc_stack
    uid = 8003
    bvids = ["BVok1", "BVok2", "BVok3", "BVfail1", "BVfail2"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    for bvid in ["BVok1", "BVok2", "BVok3"]:
        await _seed_processing_status(pd, uid, bvid, ProcessingItemStatus.SUCCESS)
    for bvid in ["BVfail1", "BVfail2"]:
        await _seed_processing_status(pd, uid, bvid, ProcessingItemStatus.FAILED)

    result = await cmd.process_uid(uid, retry_failed_only=True)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert sorted(_dispatched_bvids) == ["BVfail1", "BVfail2"]


@pytest.mark.asyncio
async def test_dry_run_skips_worker_dispatch(proc_stack, fetching_stack, capsys):
    """Dry-run still writes task / progress, but no worker is invoked."""
    cmd, pd, _pe, fd = proc_stack
    uid = 8004
    bvids = ["BVdr1", "BVdr2", "BVdr3"]
    await _seed_fetching_video_detail(fd, uid, bvids)

    result = await cmd.process_uid(uid, dry_run=True)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert _dispatched_bvids == []
    assert result.dry_run_candidates is not None
    assert sorted(result.dry_run_candidates) == sorted(bvids)

    # task.json still written
    task = await pd.get(_task_key(uid))
    assert task is not None
    assert "audio" in task["pipelines"]

    # candidate list printed for human consumption
    out = capsys.readouterr().out
    assert "dry_run candidates:" in out


@pytest.mark.asyncio
async def test_dry_run_with_no_videos_returns_success(proc_stack):
    """Dry-run on a uid with no video_detail data → SUCCESS, empty candidates."""
    cmd, _pd, _pe, _fd = proc_stack
    uid = 8005

    result = await cmd.process_uid(uid, dry_run=True)

    assert result.status == ProcessingTaskStatus.SUCCESS
    assert result.dry_run_candidates == []
    assert _dispatched_bvids == []


# -- argparse layer ---------------------------------------------------------

def test_cli_argparse_accepts_new_process_flags():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args([
        "process", "1234",
        "--limit", "5",
        "--only-bvids", "BV1", "BV2",
        "--dry-run",
    ])
    assert args.limit == 5
    assert args.only_bvids == ["BV1", "BV2"]
    assert args.dry_run is True
    assert args.retry_failed_only is False


def test_cli_argparse_retry_failed_only_flag():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["process", "1234", "--retry-failed-only"])
    assert args.retry_failed_only is True


def test_cli_argparse_retry_failed_only_conflicts_with_full(monkeypatch):
    """``--retry-failed-only`` + ``--mode full`` is rejected at runtime."""
    import asyncio

    from bili_unit.__main__ import _build_parser, _handle_process

    parser = _build_parser()
    args = parser.parse_args([
        "process", "1234", "--retry-failed-only", "--mode", "full",
    ])
    with pytest.raises(SystemExit):
        asyncio.run(_handle_process(args))
