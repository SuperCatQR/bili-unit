# tests for delete_uid across fetching / parsing / processing / BiliCommand.
#
# Coverage:
#   - fetching Command.delete_uid: clears data + errors; zero-count on missing uid
#   - parsing ParsingCommand.delete_uid: clears KV + images dir; zero-count on missing uid
#   - processing ProcessingCommand.delete_uid: clears data + errors + temp + asr_cache;
#     zero-count on missing uid
#   - BiliCommand.delete_uid: aggregates stage results

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from bili_unit.command import BiliCommand
from bili_unit.fetching.command import Command as FetchingCommand
from bili_unit.fetching.data import DataStore as FetchingDataStore
from bili_unit.fetching.error import ErrorStore as FetchingErrorStore
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.parsing.command import ParsingCommand
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.processing.command import ProcessingCommand
from bili_unit.processing.data import ProcessingDataStore
from bili_unit.processing.env import ProcessingEnv
from bili_unit.processing.error import ProcessingErrorStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_processing_settings(tmp_path: Path) -> ProcessingEnv:
    return ProcessingEnv(
        bili_processing_data_dir=str(tmp_path / "proc-data"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_error_dir=str(tmp_path / "proc-error"),
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=4,
    )


# ---------------------------------------------------------------------------
# fetching Command.delete_uid
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def fetch_cmd(tmp_path: Path):
    ds = FetchingDataStore(str(tmp_path / "f-data"))
    es = FetchingErrorStore(str(tmp_path / "f-errors"))
    await ds.open()
    await es.open()
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0, pause_seconds=0)
    cmd = FetchingCommand(ds, es, rl)
    yield cmd
    await ds.close()
    await es.close()


@pytest.mark.asyncio
async def test_fetching_delete_uid_clears_data_and_errors(fetch_cmd: FetchingCommand):
    """delete_uid removes all uid-prefixed data keys and error records."""
    ds = fetch_cmd._data
    es = fetch_cmd._error

    # Seed two uids
    await ds.put("uid:100:task", {"status": "SUCCESS"})
    await ds.put("uid:100:fetch:videos", {"data": "v"})
    await ds.put("uid:100:fetch:user_info", {"data": "u"})
    await ds.put("uid:200:task", {"status": "SUCCESS"})
    await es.record(ValueError("boom"), uid=100)
    await es.record(ValueError("boom2"), uid=100)
    await es.record(ValueError("other"), uid=200)

    result = await fetch_cmd.delete_uid(100)

    assert result["data"] == 3
    assert result["errors"] == 2

    # uid:100 data should be gone
    assert await ds.list_prefix("uid:100:") == []
    assert await es.list_records(uid=100) == []

    # uid:200 data should survive
    assert await ds.get("uid:200:task") is not None
    assert len(await es.list_records(uid=200)) == 1


@pytest.mark.asyncio
async def test_fetching_delete_uid_zero_count_on_missing(fetch_cmd: FetchingCommand):
    """delete_uid on a uid with no data returns zero counts and does not raise."""
    result = await fetch_cmd.delete_uid(999)
    assert result == {"data": 0, "errors": 0}


# ---------------------------------------------------------------------------
# parsing ParsingCommand.delete_uid
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def parsing_cmd(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "p-data")
    await ds.open()
    fetch_qry = MagicMock()
    cmd = ParsingCommand(ds, fetch_qry)
    yield cmd
    await ds.close()


@pytest.mark.asyncio
async def test_parsing_delete_uid_clears_kv_and_images(parsing_cmd: ParsingCommand, tmp_path: Path):
    """delete_uid removes all uid-prefixed KV entries and the images directory."""
    ds = parsing_cmd._data

    # Seed KV for uid=10 and uid=20
    await ds.put("uid:10:task", {"status": "SUCCESS"})
    await ds.put("uid:10:parse:video_work:BV001", {"title": "T"})
    await ds.put("uid:20:task", {"status": "SUCCESS"})

    # Seed images directory for uid=10
    images_dir: Path = ds.base / "10" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "cover.jpg").write_bytes(b"fake")

    result = await parsing_cmd.delete_uid(10)

    assert result["data"] == 2
    assert result["images_dir_removed"] == 1
    assert not images_dir.exists()

    # uid:20 should survive
    assert await ds.get("uid:20:task") is not None
    assert await ds.list_prefix("uid:10:") == []


@pytest.mark.asyncio
async def test_parsing_delete_uid_no_images_dir(parsing_cmd: ParsingCommand):
    """delete_uid works when no images directory exists."""
    ds = parsing_cmd._data
    await ds.put("uid:10:task", {"status": "SUCCESS"})

    result = await parsing_cmd.delete_uid(10)

    assert result["data"] == 1
    assert result["images_dir_removed"] == 0


@pytest.mark.asyncio
async def test_parsing_delete_uid_zero_count_on_missing(parsing_cmd: ParsingCommand):
    """delete_uid on a uid with no data returns zero counts and does not raise."""
    result = await parsing_cmd.delete_uid(999)
    assert result == {"data": 0, "images_dir_removed": 0}


# ---------------------------------------------------------------------------
# processing ProcessingCommand.delete_uid
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def proc_cmd(tmp_path: Path):
    settings = _make_processing_settings(tmp_path)
    ds = ProcessingDataStore(settings.bili_processing_data_dir)
    es = ProcessingErrorStore(settings.bili_processing_error_dir)
    await ds.open()
    await es.open()
    fetch_qry = MagicMock()
    cmd = ProcessingCommand(
        data=ds,
        error=es,
        temp_dir=settings.bili_processing_temp_dir,
        fetching_query=fetch_qry,
        settings=settings,
    )
    yield cmd
    await ds.close()
    await es.close()


@pytest.mark.asyncio
async def test_processing_delete_uid_clears_all(proc_cmd: ProcessingCommand, tmp_path: Path):
    """delete_uid removes data, errors, temp dir, and asr_cache dir for the uid."""
    ds = proc_cmd._data
    es = proc_cmd._error
    settings = proc_cmd._settings

    # Seed data for uid=42 and uid=43
    await ds.put("uid:42:task", {"status": "SUCCESS"})
    await ds.put("uid:42:proc:audio:BVaaa", {"status": "SUCCESS"})
    await ds.put("uid:43:task", {"status": "SUCCESS"})
    await es.record(ValueError("audio fail"), uid=42, pipeline="audio", item_type="transcription", item_id="BVaaa")
    await es.record(ValueError("other"), uid=43)

    # Seed temp dir for uid=42
    temp_uid_dir = Path(settings.bili_processing_temp_dir) / "42"
    temp_uid_dir.mkdir(parents=True, exist_ok=True)
    (temp_uid_dir / "audio.m4a").write_bytes(b"fake")

    # Seed ASR cache dir for uid=42
    asr_uid_dir = Path(settings.bili_processing_asr_cache_dir) / "42" / "BVaaa"
    asr_uid_dir.mkdir(parents=True, exist_ok=True)
    (asr_uid_dir / "0.json").write_text("{}", encoding="utf-8")

    result = await proc_cmd.delete_uid(42)

    assert result["data"] == 2
    assert result["errors"] == 1
    assert result["temp_removed"] == 1
    assert result["asr_cache_removed"] == 1

    # uid:42 should be gone
    assert await ds.list_prefix("uid:42:") == []
    assert await es.list_records(uid=42) == []
    assert not temp_uid_dir.exists()
    assert not (Path(settings.bili_processing_asr_cache_dir) / "42").exists()

    # uid:43 should survive
    assert await ds.get("uid:43:task") is not None
    assert len(await es.list_records(uid=43)) == 1


@pytest.mark.asyncio
async def test_processing_delete_uid_no_dirs(proc_cmd: ProcessingCommand):
    """delete_uid works when no temp/asr_cache directories exist."""
    ds = proc_cmd._data
    await ds.put("uid:42:task", {"status": "SUCCESS"})

    result = await proc_cmd.delete_uid(42)

    assert result["data"] == 1
    assert result["errors"] == 0
    assert result["temp_removed"] == 0
    assert result["asr_cache_removed"] == 0


@pytest.mark.asyncio
async def test_processing_delete_uid_zero_count_on_missing(proc_cmd: ProcessingCommand):
    """delete_uid on a uid with no data returns zero counts and does not raise."""
    result = await proc_cmd.delete_uid(999)
    assert result == {"data": 0, "errors": 0, "temp_removed": 0, "asr_cache_removed": 0}


# ---------------------------------------------------------------------------
# BiliCommand.delete_uid
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def bili_cmd(tmp_path: Path):
    # Fetching
    f_ds = FetchingDataStore(str(tmp_path / "f-data"))
    f_es = FetchingErrorStore(str(tmp_path / "f-errors"))
    await f_ds.open()
    await f_es.open()
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0, pause_seconds=0)
    fetch_cmd = FetchingCommand(f_ds, f_es, rl)

    # Parsing
    p_ds = ParsingDataStore(tmp_path / "p-data")
    await p_ds.open()
    fetch_qry = MagicMock()
    parse_cmd = ParsingCommand(p_ds, fetch_qry)

    # Processing
    settings = _make_processing_settings(tmp_path)
    proc_ds = ProcessingDataStore(settings.bili_processing_data_dir)
    proc_es = ProcessingErrorStore(settings.bili_processing_error_dir)
    await proc_ds.open()
    await proc_es.open()
    proc_cmd = ProcessingCommand(
        data=proc_ds,
        error=proc_es,
        temp_dir=settings.bili_processing_temp_dir,
        fetching_query=MagicMock(),
        settings=settings,
    )

    cmd = BiliCommand(fetch_cmd, parsing=parse_cmd, processing=proc_cmd)
    yield cmd, f_ds, p_ds, proc_ds, proc_es
    await cmd.close()


@pytest.mark.asyncio
async def test_bili_command_delete_uid_aggregates_stages(bili_cmd):
    """BiliCommand.delete_uid returns per-stage stats and clears all stages."""
    cmd, f_ds, p_ds, proc_ds, proc_es = bili_cmd

    # Seed fetching
    await f_ds.put("uid:7:task", {"status": "SUCCESS"})

    # Seed parsing
    await p_ds.put("uid:7:task", {"status": "SUCCESS"})
    await p_ds.put("uid:7:parse:video_work:BV007", {"title": "T"})

    # Seed processing
    await proc_ds.put("uid:7:task", {"status": "SUCCESS"})
    await proc_es.record(ValueError("e"), uid=7, pipeline="audio", item_type="t", item_id="BV007")

    stats = await cmd.delete_uid(7)

    assert set(stats.keys()) == {"fetching", "parsing", "processing"}
    assert stats["fetching"]["data"] == 1
    assert stats["fetching"]["errors"] == 0
    assert stats["parsing"]["data"] == 2
    assert stats["parsing"]["images_dir_removed"] == 0
    assert stats["processing"]["data"] == 1
    assert stats["processing"]["errors"] == 1

    # Verify everything cleared
    assert await f_ds.list_prefix("uid:7:") == []
    assert await p_ds.list_prefix("uid:7:") == []
    assert await proc_ds.list_prefix("uid:7:") == []
