"""Tests for ``BiliCommand.delete_uid`` and per-stage ``delete_uid``.

Phase 6 rewrite. Background:

The pre-refactor implementation routed ``delete_uid`` through each stage's
data + error stores (file-directory KV). The SQLite refactor inverted this:

  * ``BiliCommand.delete_uid(uid)`` is now pure FILE IO.  It deletes
    ``{db_dir}/{uid}.db`` (main DB), ``{db_dir}/{uid}.raw.db`` (raw DB), and
    ``{db_dir}/{uid}/`` (workdir for images / audio caches), returning
    ``{"main_db": 0|1, "raw_db": 0|1, "workdir_files": N}``.
  * Per-stage ``Command.delete_uid`` is a no-op returning ``{}``; the unit
    handles cleanup at the file-IO layer instead of routing through
    per-stage stores.

The tests below verify file deletion, idempotency, that other uids survive,
WAL companion-file cleanup, and the per-stage no-op contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.command import BiliCommand
from bili_unit.fetching.command import Command as FetchingCommand
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.parsing.command import ParsingCommand
from bili_unit.processing.command import ProcessingCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "proc-temp"),
        bili_processing_asr_cache_dir=str(tmp_path / "proc-asr-cache"),
        bili_processing_asr_backend="mock",
        bili_processing_audio_workers=1,
        bili_processing_queue_maxsize=4,
    )


async def _seed_uid(uid: int, settings: BiliSettings, *, with_workdir: bool = True) -> None:
    """Open + close a UidContext to materialise both DBs, then optionally
    drop a couple of files in the workdir to verify recursive removal."""
    ctx = UidContext(uid, settings.bili_db_dir)
    await ctx.open()
    await ctx.close()
    if with_workdir:
        workdir = Path(settings.bili_db_dir) / str(uid)
        (workdir / "images").mkdir(parents=True, exist_ok=True)
        (workdir / "images" / "cover.jpg").write_bytes(b"fake")
        (workdir / "audio").mkdir(parents=True, exist_ok=True)
        (workdir / "audio" / "BV001.m4a").write_bytes(b"fake")


# ---------------------------------------------------------------------------
# BiliCommand.delete_uid — primary contract
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def bili_cmd(tmp_path: Path):
    settings = _make_settings(tmp_path)
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0)
    fetch_cmd = FetchingCommand(settings, rl)
    parse_cmd = ParsingCommand(settings)
    proc_cmd = ProcessingCommand(settings)

    cmd = BiliCommand(
        fetch_cmd,
        parsing=parse_cmd,
        processing=proc_cmd,
        settings=settings,
    )
    try:
        yield cmd, settings
    finally:
        await cmd.close()


async def test_bili_command_delete_uid_removes_main_raw_workdir(
    bili_cmd, tmp_path: Path,
) -> None:
    """Happy path: both DBs and a populated workdir all get removed; the
    returned counts match the on-disk reality."""
    cmd, settings = bili_cmd
    uid = 42
    await _seed_uid(uid, settings)

    main_db = Path(settings.bili_db_dir) / f"{uid}.db"
    raw_db = Path(settings.bili_db_dir) / f"{uid}.raw.db"
    workdir = Path(settings.bili_db_dir) / str(uid)
    assert main_db.exists()
    assert raw_db.exists()
    assert workdir.exists()

    stats = await cmd.delete_uid(uid)

    assert stats == {"main_db": 1, "raw_db": 1, "workdir_files": 2}
    assert not main_db.exists()
    assert not raw_db.exists()
    assert not workdir.exists()


async def test_bili_command_delete_uid_other_uids_survive(
    bili_cmd,
) -> None:
    """Deleting one uid leaves another uid's files untouched."""
    cmd, settings = bili_cmd
    target_uid = 100
    bystander_uid = 200

    await _seed_uid(target_uid, settings)
    await _seed_uid(bystander_uid, settings)

    bystander_main = Path(settings.bili_db_dir) / f"{bystander_uid}.db"
    bystander_raw = Path(settings.bili_db_dir) / f"{bystander_uid}.raw.db"
    bystander_workdir = Path(settings.bili_db_dir) / str(bystander_uid)

    stats = await cmd.delete_uid(target_uid)
    assert stats["main_db"] == 1
    assert stats["raw_db"] == 1

    assert bystander_main.exists()
    assert bystander_raw.exists()
    assert bystander_workdir.exists()


async def test_bili_command_delete_uid_idempotent_on_missing(bili_cmd) -> None:
    """Deleting a uid that has no files returns zeros and does not raise."""
    cmd, _settings = bili_cmd
    stats = await cmd.delete_uid(999)
    assert stats == {"main_db": 0, "raw_db": 0, "workdir_files": 0}


async def test_bili_command_delete_uid_idempotent_on_repeat(bili_cmd) -> None:
    """Calling delete_uid twice in a row: first call cleans up, second
    call is a no-op returning zeros."""
    cmd, settings = bili_cmd
    uid = 77
    await _seed_uid(uid, settings)

    first = await cmd.delete_uid(uid)
    assert first["main_db"] == 1
    assert first["raw_db"] == 1

    second = await cmd.delete_uid(uid)
    assert second == {"main_db": 0, "raw_db": 0, "workdir_files": 0}


async def test_bili_command_delete_uid_no_workdir(bili_cmd) -> None:
    """A uid that has DBs but no workdir reports ``workdir_files=0``."""
    cmd, settings = bili_cmd
    uid = 55
    await _seed_uid(uid, settings, with_workdir=False)

    stats = await cmd.delete_uid(uid)
    assert stats["main_db"] == 1
    assert stats["raw_db"] == 1
    assert stats["workdir_files"] == 0


async def test_bili_command_delete_uid_cleans_wal_companions(
    bili_cmd, tmp_path: Path,
) -> None:
    """SQLite -wal / -shm sidecar files must also be removed when the main
    DB is deleted, otherwise re-opening would silently restore old state."""
    cmd, settings = bili_cmd
    uid = 88
    await _seed_uid(uid, settings)

    main_db = Path(settings.bili_db_dir) / f"{uid}.db"
    raw_db = Path(settings.bili_db_dir) / f"{uid}.raw.db"
    # Simulate WAL companions (the connection layer would normally produce
    # them under load; touch synthetic ones for the test).
    main_wal = main_db.with_name(main_db.name + "-wal")
    main_shm = main_db.with_name(main_db.name + "-shm")
    raw_wal = raw_db.with_name(raw_db.name + "-wal")
    main_wal.write_bytes(b"")
    main_shm.write_bytes(b"")
    raw_wal.write_bytes(b"")

    await cmd.delete_uid(uid)

    assert not main_wal.exists()
    assert not main_shm.exists()
    assert not raw_wal.exists()


async def test_bili_command_delete_uid_requires_settings(tmp_path: Path) -> None:
    """Constructing a BiliCommand without settings makes delete_uid raise —
    we have no idea where the files live without a db_dir."""
    settings = _make_settings(tmp_path)
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0)
    fetch_cmd = FetchingCommand(settings, rl)
    cmd = BiliCommand(fetch_cmd)  # no settings= kw — delete_uid must refuse
    try:
        with pytest.raises(RuntimeError, match="settings"):
            await cmd.delete_uid(7)
    finally:
        await cmd.close()


# ---------------------------------------------------------------------------
# Per-stage delete_uid — no-op contract
# ---------------------------------------------------------------------------

async def test_fetching_command_delete_uid_is_noop(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    rl = RateLimitController(global_qps=10.0, endpoint_qps=10.0)
    cmd = FetchingCommand(settings, rl)
    try:
        result = await cmd.delete_uid(123)
        assert result == {}
    finally:
        await cmd.close()


async def test_parsing_command_delete_uid_is_noop(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    cmd = ParsingCommand(settings)
    try:
        result = await cmd.delete_uid(123)
        assert result == {}
    finally:
        await cmd.close()


async def test_processing_command_delete_uid_is_noop(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    cmd = ProcessingCommand(settings)
    try:
        result = await cmd.delete_uid(123)
        assert result == {}
    finally:
        await cmd.close()
