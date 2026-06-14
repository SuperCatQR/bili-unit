# Tests for the W4 manifest module.
#
# Covers:
#   1. Empty state — compute_manifest with no stage data renders None slots.
#   2. fetching only — fetching slot present, parsing/processing None.
#   3. All three stages — every slot populated and consistent.
#   4. Cost rollup — audio cost dicts accumulate correctly + subtitle/asr split.
#   5. Completeness — ratio of is_complete=True / total per model.
#   6. write/read/delete round-trip on disk.
#   7. CLI manifest sub-command via the in-process handler.

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bili_unit._env import BiliSettings
from bili_unit._manifest import (
    compute_manifest,
    delete_manifest,
    read_manifest,
    write_manifest,
)
from bili_unit.command import BiliCommand
from bili_unit.fetching import EndpointStatus, TaskStatus
from bili_unit.fetching.command import Command as FetchingCommand
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.keys import _fetch_key
from bili_unit.fetching.keys import _task_key as _fetch_task_key
from bili_unit.fetching.query import Query as FetchingQuery
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.task import EndpointEntry, TaskValue
from bili_unit.parsing import (
    ParsingModelStatus,
    ParsingTaskStatus,
    ParsingTaskValue,
)
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key as _parsing_item_key
from bili_unit.parsing.keys import _task_key as _parsing_task_key
from bili_unit.parsing.query import ParsingQuery
from bili_unit.processing import (
    ProcessingItemStatus,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.error import ProcessingErrorStore
from bili_unit.processing.keys import _proc_key
from bili_unit.processing.keys import _task_key as _proc_task_key
from bili_unit.processing.query import ProcessingQuery
from bili_unit.processing.task import (
    PipelineEntry,
    ProcessingTaskValue,
)
from bili_unit.query import BiliQuery

# ---------------------------------------------------------------------------
# Fixtures: a thin BiliQuery wired to in-memory stores. Stages are wrapped
# into a BiliQuery so that compute_manifest can be tested end-to-end.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def stack(tmp_path: Path):
    fd = FetchingDataStore(str(tmp_path / "fetch-data"))
    fe = FetchingErrorStore(str(tmp_path / "fetch-error"))
    pd = ParsingDataStore(str(tmp_path / "parse-data"))
    procd = ProcessingDataStore(str(tmp_path / "proc-data"))
    proce = ProcessingErrorStore(str(tmp_path / "proc-error"))
    await fd.open()
    await fe.open()
    await pd.open()
    await procd.open()
    await proce.open()

    fqry = FetchingQuery(fd, fe)
    pqry = ParsingQuery(data=pd)
    procqry = ProcessingQuery(data=procd, error=proce)
    bqry = BiliQuery(fqry, parsing=pqry, processing=procqry)

    yield {
        "fd": fd, "fe": fe, "pd": pd, "procd": procd, "proce": proce,
        "fqry": fqry, "pqry": pqry, "procqry": procqry, "bqry": bqry,
        "tmp_path": tmp_path,
    }

    await fd.close()
    await fe.close()
    await pd.close()
    await procd.close()
    await proce.close()


# ---------------------------------------------------------------------------
# Helpers that seed task / item rows directly through the data stores.
# ---------------------------------------------------------------------------

async def _seed_fetching_task(
    fd: FetchingDataStore,
    uid: int,
    *,
    endpoint_status: dict[str, EndpointStatus],
    task_status: TaskStatus = TaskStatus.SUCCESS,
    failed_item_ids: list[str] | None = None,
) -> None:
    tv = TaskValue(
        uid=uid,
        status=task_status,
        endpoints={
            name: EndpointEntry(status=status)
            for name, status in endpoint_status.items()
        },
        created_at=1_700_000_000_000,
        updated_at=1_700_000_001_000,
        failed_item_ids=failed_item_ids or [],
    )
    await fd.put(_fetch_task_key(uid), tv.to_dict())
    # also write the per-endpoint fetch_key so query.get_endpoint sees them
    for name, status in endpoint_status.items():
        await fd.put(_fetch_key(uid, name), {
            "uid": uid, "endpoint": name, "status": status.value,
            "raw_payload": {} if status == EndpointStatus.SUCCESS else None,
        })


async def _seed_parsing_task(
    pd: ParsingDataStore,
    uid: int,
    *,
    models: dict[str, dict],
    task_status: ParsingTaskStatus = ParsingTaskStatus.SUCCESS,
    images: dict | None = None,
    failed_item_ids: list[str] | None = None,
) -> None:
    tv = ParsingTaskValue(
        uid=uid,
        status=task_status,
        models=models,
        images=images,
        created_at=1_700_000_002_000,
        updated_at=1_700_000_003_000,
        failed_item_ids=failed_item_ids or [],
    )
    await pd.put(_parsing_task_key(uid), tv.to_dict())


async def _seed_parsing_item(
    pd: ParsingDataStore,
    uid: int,
    model: str,
    item_id: str,
    *,
    is_complete: bool,
    extra: dict | None = None,
) -> None:
    payload = {
        "_model_name": model,
        "_schema_version": 1,
        "is_complete": is_complete,
    }
    if extra:
        payload.update(extra)
    await pd.put(_parsing_item_key(uid, model, item_id), payload)


async def _seed_processing_task(
    procd: ProcessingDataStore,
    uid: int,
    *,
    pipelines: dict[str, PipelineEntry],
    task_status: ProcessingTaskStatus = ProcessingTaskStatus.SUCCESS,
    failed_item_ids: list[str] | None = None,
) -> None:
    tv = ProcessingTaskValue(
        uid=uid,
        status=task_status,
        pipelines=pipelines,
        created_at=1_700_000_004_000,
        updated_at=1_700_000_005_000,
        failed_item_ids=failed_item_ids or [],
    )
    await procd.put(_proc_task_key(uid), tv.to_dict())


async def _seed_audio_item(
    procd: ProcessingDataStore,
    uid: int,
    bvid: str,
    *,
    transcription_source: str,
    cost: dict,
    status: ProcessingItemStatus = ProcessingItemStatus.SUCCESS,
) -> None:
    await procd.put(_proc_key(uid, "audio", bvid), {
        "uid": uid,
        "pipeline": "audio",
        "item_type": "transcription",
        "item_id": bvid,
        "status": status.value,
        "result": {
            "bvid": bvid,
            "pages": [],
            "total_duration": 0.0,
            "total_chars": 0,
            "transcription_source": transcription_source,
            "cost": cost,
        },
    })


# ---------------------------------------------------------------------------
# 1. Empty state — every stage absent.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_manifest_empty_state(stack):
    manifest = await compute_manifest(uid=42, qry=stack["bqry"])
    assert manifest["uid"] == 42
    assert manifest["schema_version"] == 1
    assert isinstance(manifest["computed_at"], int)
    assert manifest["fetching"] is None
    assert manifest["parsing"] is None
    assert manifest["processing"] is None
    # cost has no audio items → None
    assert manifest["cost"] is None
    # parsing query exists, but no models → completeness is None
    assert manifest["completeness"] is None


# ---------------------------------------------------------------------------
# 2. Fetching present, parsing/processing absent.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_manifest_fetching_only(stack):
    uid = 7
    await _seed_fetching_task(
        stack["fd"], uid,
        endpoint_status={
            "user_info": EndpointStatus.SUCCESS,
            "videos": EndpointStatus.SUCCESS,
            "broken_ep": EndpointStatus.FAILED_EXHAUSTED,
        },
        failed_item_ids=["broken_ep"],
    )
    manifest = await compute_manifest(uid=uid, qry=stack["bqry"])
    f = manifest["fetching"]
    assert f is not None
    assert f["status"] == TaskStatus.SUCCESS.value
    assert f["endpoint_count"] == 3
    assert f["success_count"] == 2
    assert f["failed_count"] == 1
    assert f["failed_item_ids"] == ["broken_ep"]
    assert manifest["parsing"] is None
    assert manifest["processing"] is None


# ---------------------------------------------------------------------------
# 3. All three stages populated.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_manifest_full_three_stages(stack):
    uid = 100
    # fetching
    await _seed_fetching_task(
        stack["fd"], uid,
        endpoint_status={
            "user_info": EndpointStatus.SUCCESS,
            "videos": EndpointStatus.SUCCESS,
        },
    )
    # parsing
    await _seed_parsing_task(
        stack["pd"], uid,
        models={
            "user_profile": {"status": ParsingModelStatus.SUCCESS.value, "count": 1},
            "video_work": {"status": ParsingModelStatus.SUCCESS.value, "count": 2},
        },
        images={"total": 4, "ok": 3, "skipped": 1, "failed": 0, "failed_urls": []},
    )
    await _seed_parsing_item(
        stack["pd"], uid, "user_profile", str(uid), is_complete=True,
    )
    await _seed_parsing_item(
        stack["pd"], uid, "video_work", "BV1", is_complete=True,
    )
    await _seed_parsing_item(
        stack["pd"], uid, "video_work", "BV2", is_complete=False,
    )
    # processing
    await _seed_processing_task(
        stack["procd"], uid,
        pipelines={
            "audio": PipelineEntry(
                status=ProcessingPipelineStatus.SUCCESS,
                items={
                    "transcription": {
                        "total": 2, "completed": 2, "failed": 0, "skipped": 0,
                    },
                },
            ),
        },
    )
    await _seed_audio_item(
        stack["procd"], uid, "BV1",
        transcription_source="subtitle",
        cost={"audio_tokens": 0, "seconds": 0,
              "model": "subtitle", "cache_hits": 0},
    )
    await _seed_audio_item(
        stack["procd"], uid, "BV2",
        transcription_source="asr",
        cost={"audio_tokens": 800, "seconds": 120,
              "model": "m", "cache_hits": 0},
    )

    manifest = await compute_manifest(uid=uid, qry=stack["bqry"])

    # fetching slot
    assert manifest["fetching"]["endpoint_count"] == 2
    assert manifest["fetching"]["success_count"] == 2

    # parsing slot
    p = manifest["parsing"]
    assert p["status"] == ParsingTaskStatus.SUCCESS.value
    assert p["models"]["user_profile"] == {
        "count": 1, "complete_count": 1,
        "status": ParsingModelStatus.SUCCESS.value,
    }
    assert p["models"]["video_work"]["complete_count"] == 1  # BV1 only
    assert p["images"]["total"] == 4

    # processing slot
    proc = manifest["processing"]
    assert proc["status"] == ProcessingTaskStatus.SUCCESS.value
    audio_pipe = proc["pipelines"]["audio"]
    assert audio_pipe["status"] == ProcessingPipelineStatus.SUCCESS.value
    transcription = audio_pipe["transcription"]
    assert transcription["total"] == 2
    assert transcription["subtitle_source"] == 1
    assert transcription["asr_source"] == 1

    # cost rollup
    cost = manifest["cost"]
    assert cost["total_audio_tokens"] == 800
    assert cost["total_seconds"] == 120
    assert cost["asr_calls"] == 1
    assert cost["subtitle_count"] == 1
    assert cost["cache_hits"] == 0

    # completeness
    comp = manifest["completeness"]
    assert comp["user_profile"] == 1.0
    assert comp["video_work"] == 0.5


# ---------------------------------------------------------------------------
# 4. Cost rollup — multiple audio items mix subtitle / ASR / cache.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_manifest_cost_aggregates_correctly(stack):
    uid = 200
    await _seed_processing_task(
        stack["procd"], uid,
        pipelines={
            "audio": PipelineEntry(
                status=ProcessingPipelineStatus.SUCCESS,
                items={
                    "transcription": {
                        "total": 4, "completed": 4, "failed": 0, "skipped": 0,
                    },
                },
            ),
        },
    )
    # 2 ASR calls, 2 subtitle short-circuits.
    await _seed_audio_item(
        stack["procd"], uid, "BVa",
        transcription_source="asr",
        cost={"audio_tokens": 100, "seconds": 50,
              "model": "m", "cache_hits": 0},
    )
    await _seed_audio_item(
        stack["procd"], uid, "BVb",
        transcription_source="asr",
        cost={"audio_tokens": 200, "seconds": 80,
              "model": "m", "cache_hits": 3},
    )
    await _seed_audio_item(
        stack["procd"], uid, "BVc",
        transcription_source="subtitle",
        cost={"audio_tokens": 0, "seconds": 0,
              "model": "subtitle", "cache_hits": 0},
    )
    await _seed_audio_item(
        stack["procd"], uid, "BVd",
        transcription_source="subtitle",
        cost={"audio_tokens": 0, "seconds": 0,
              "model": "subtitle", "cache_hits": 0},
    )

    manifest = await compute_manifest(uid=uid, qry=stack["bqry"])
    cost = manifest["cost"]
    assert cost["total_audio_tokens"] == 300
    assert cost["total_seconds"] == 130
    assert cost["asr_calls"] == 2
    assert cost["subtitle_count"] == 2
    assert cost["cache_hits"] == 3


# ---------------------------------------------------------------------------
# 5. Completeness — half of video_work is_complete=True.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_manifest_completeness_video_work_half(stack):
    uid = 300
    await _seed_parsing_task(
        stack["pd"], uid,
        models={
            "video_work": {"status": ParsingModelStatus.SUCCESS.value, "count": 4},
        },
    )
    await _seed_parsing_item(stack["pd"], uid, "video_work", "BV1", is_complete=True)
    await _seed_parsing_item(stack["pd"], uid, "video_work", "BV2", is_complete=True)
    await _seed_parsing_item(stack["pd"], uid, "video_work", "BV3", is_complete=False)
    await _seed_parsing_item(stack["pd"], uid, "video_work", "BV4", is_complete=False)
    # add a video_subtitle for BV1 only — video_subtitle coverage = 1/4
    await _seed_parsing_item(
        stack["pd"], uid, "video_subtitle", "BV1", is_complete=True,
    )

    manifest = await compute_manifest(uid=uid, qry=stack["bqry"])
    comp = manifest["completeness"]
    assert comp["video_work"] == pytest.approx(0.5)
    assert comp["video_subtitle"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 6. Disk round trip: write → read → delete → read.
# ---------------------------------------------------------------------------

def test_manifest_disk_roundtrip(tmp_path: Path):
    manifest_dir = tmp_path / "manifests"
    sample = {"uid": 12, "schema_version": 1, "fetching": None}
    write_manifest(12, manifest_dir, sample)
    file_path = manifest_dir / "12.json"
    assert file_path.exists()

    loaded = read_manifest(12, manifest_dir)
    assert loaded == sample

    deleted = delete_manifest(12, manifest_dir)
    assert deleted is True
    assert not file_path.exists()
    assert read_manifest(12, manifest_dir) is None
    # double-delete is a no-op (False).
    assert delete_manifest(12, manifest_dir) is False


# ---------------------------------------------------------------------------
# 7. CLI manifest sub-command.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cli_manifest_prints_summary(tmp_path: Path):
    """``python -m bili_unit manifest <uid>`` prints uid + key fields."""
    from bili_unit.__main__ import _handle_manifest

    manifest_dir = tmp_path / "manifest"
    manifest = {
        "uid": 999,
        "schema_version": 1,
        "computed_at": 0,
        "fetching": {
            "status": "SUCCESS", "endpoint_count": 3,
            "success_count": 3, "failed_count": 0,
            "failed_item_ids": [], "updated_at": 0,
        },
        "parsing": None,
        "processing": None,
        "cost": None,
        "completeness": None,
    }
    write_manifest(999, manifest_dir, manifest)

    settings = BiliSettings(bili_manifest_dir=str(manifest_dir))

    class _Args:
        uid = 999
        json = False

    buf = io.StringIO()
    with patch("bili_unit._env.get_settings", return_value=settings), \
         redirect_stdout(buf):
        await _handle_manifest(_Args())

    out = buf.getvalue()
    assert "uid=999" in out
    assert "fetching" in out
    assert "endpoints=3" in out


@pytest.mark.asyncio
async def test_cli_manifest_json_flag(tmp_path: Path):
    from bili_unit.__main__ import _handle_manifest

    manifest_dir = tmp_path / "manifest"
    manifest = {"uid": 555, "schema_version": 1, "fetching": None}
    write_manifest(555, manifest_dir, manifest)
    settings = BiliSettings(bili_manifest_dir=str(manifest_dir))

    class _Args:
        uid = 555
        json = True

    buf = io.StringIO()
    with patch("bili_unit._env.get_settings", return_value=settings), \
         redirect_stdout(buf):
        await _handle_manifest(_Args())

    parsed = json.loads(buf.getvalue())
    assert parsed["uid"] == 555


@pytest.mark.asyncio
async def test_cli_manifest_missing_prints_hint(tmp_path: Path):
    from bili_unit.__main__ import _handle_manifest

    manifest_dir = tmp_path / "manifest"
    settings = BiliSettings(bili_manifest_dir=str(manifest_dir))

    class _Args:
        uid = 1
        json = False

    buf = io.StringIO()
    with patch("bili_unit._env.get_settings", return_value=settings), \
         redirect_stdout(buf):
        await _handle_manifest(_Args())

    out = buf.getvalue()
    assert "未生成" in out
    assert "uid=1" in out


# ---------------------------------------------------------------------------
# 8. BiliCommand integration: persist + delete via the unit-level wrapper.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def bili_cmd_with_manifest(tmp_path: Path, stack):
    """Construct a BiliCommand with a real BiliQuery + settings so the
    persist hook can run end-to-end against the in-memory stack stores."""
    settings = BiliSettings(
        bili_fetching_data_dir=str(tmp_path / "f-data"),
        bili_fetching_error_dir=str(tmp_path / "f-error"),
        bili_parsing_data_dir=str(tmp_path / "p-data"),
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr-cache"),
        bili_manifest_dir=str(tmp_path / "manifests"),
    )
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0, pause_seconds=0)
    fetch_cmd = FetchingCommand(stack["fd"], stack["fe"], rl, settings)

    # Stub parsing + processing commands so delete_uid is callable but
    # fetching is the only stage we exercise here.
    parsing_cmd = AsyncMock()
    parsing_cmd.delete_uid = AsyncMock(return_value={"data": 0})
    parsing_cmd.close = AsyncMock(return_value=None)
    processing_cmd = AsyncMock()
    processing_cmd.delete_uid = AsyncMock(
        return_value={"data": 0, "errors": 0, "temp_removed": 0,
                      "asr_cache_removed": 0},
    )
    processing_cmd.close = AsyncMock(return_value=None)

    cmd = BiliCommand(
        fetch_cmd,
        parsing=parsing_cmd,
        processing=processing_cmd,
        query=stack["bqry"],
        settings=settings,
    )
    yield cmd, settings


@pytest.mark.asyncio
async def test_bili_command_delete_removes_manifest(bili_cmd_with_manifest):
    cmd, settings = bili_cmd_with_manifest
    write_manifest(77, settings.bili_manifest_dir, {"uid": 77})
    assert read_manifest(77, settings.bili_manifest_dir) is not None

    await cmd.delete_uid(77)
    assert read_manifest(77, settings.bili_manifest_dir) is None
