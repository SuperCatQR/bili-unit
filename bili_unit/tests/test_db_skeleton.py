# Smoke test for the _db skeleton: open / re-open / DDL idempotence /
# schema_version handling. No stage code touched yet.

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from bili_unit._db import (
    SUPPORTED_MAIN_SCHEMA_VERSION,
    SUPPORTED_RAW_SCHEMA_VERSION,
    Connection,
    SchemaMismatchError,
    list_uids,
    open_main,
    open_raw,
    resolve,
)
from bili_unit._db.paths import MAIN_DB_SUFFIX, RAW_DB_SUFFIX

# ---------------------------------------------------------------------------
# paths.resolve / list_uids
# ---------------------------------------------------------------------------

def test_resolve_layout(tmp_path: Path) -> None:
    paths = resolve(123, tmp_path)
    assert paths.uid == 123
    assert paths.main_db == tmp_path / f"123{MAIN_DB_SUFFIX}"
    assert paths.raw_db == tmp_path / f"123{RAW_DB_SUFFIX}"
    assert paths.workdir == tmp_path / "123"
    assert paths.images_dir == tmp_path / "123" / "images"
    assert paths.audio_dir == tmp_path / "123" / "audio"


def test_resolve_rejects_nonpositive_uid(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve(0, tmp_path)
    with pytest.raises(ValueError):
        resolve(-1, tmp_path)


def test_list_uids_empty_root(tmp_path: Path) -> None:
    assert list_uids(tmp_path) == []
    assert list_uids(tmp_path / "missing") == []


def test_list_uids_filters_raw_and_foreign(tmp_path: Path) -> None:
    (tmp_path / f"100{MAIN_DB_SUFFIX}").touch()
    (tmp_path / f"100{RAW_DB_SUFFIX}").touch()
    (tmp_path / f"42{MAIN_DB_SUFFIX}").touch()
    (tmp_path / f"abc{MAIN_DB_SUFFIX}").touch()  # non-int stem, ignored
    (tmp_path / "README.md").touch()
    assert list_uids(tmp_path) == [42, 100]


# ---------------------------------------------------------------------------
# Connection.open() — main DB
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def main_conn(tmp_path: Path):
    conn = await open_main(7, tmp_path)
    try:
        yield conn
    finally:
        await conn.close()


async def test_open_main_creates_file_and_seeds_meta(
    tmp_path: Path, main_conn: Connection,
) -> None:
    assert (tmp_path / f"7{MAIN_DB_SUFFIX}").is_file()

    version = await main_conn.fetch_value(
        "SELECT value FROM meta WHERE key = 'schema_version'",
    )
    assert int(version) == SUPPORTED_MAIN_SCHEMA_VERSION

    uid = await main_conn.fetch_value("SELECT value FROM meta WHERE key = 'uid'")
    assert int(uid) == 7

    created = await main_conn.fetch_value(
        "SELECT value FROM meta WHERE key = 'created_at_ms'",
    )
    assert int(created) > 0


async def test_open_main_creates_all_content_tables(main_conn: Connection) -> None:
    rows = await main_conn.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    names = {r["name"] for r in rows}
    expected = {
        "meta",
        "user_profile", "video", "video_page", "video_subtitle",
        "article", "opus_post", "dynamic_event",
        "audio_transcription", "image_asset",
        "stage_task", "fetch_endpoint_state", "stage_error",
    }
    missing = expected - names
    assert not missing, f"missing tables: {missing}"


async def test_open_main_creates_views(main_conn: Connection) -> None:
    rows = await main_conn.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name",
    )
    names = {r["name"] for r in rows}
    assert names == {"video_full", "manifest_summary"}


async def test_manifest_summary_view_works_on_empty_db(
    main_conn: Connection,
) -> None:
    row = await main_conn.fetch_one("SELECT * FROM manifest_summary")
    assert row is not None
    assert int(row["uid"]) == 7
    assert int(row["schema_version"]) == SUPPORTED_MAIN_SCHEMA_VERSION
    assert row["video_count"] == 0
    assert row["transcribed_count"] == 0


# ---------------------------------------------------------------------------
# Re-open idempotence
# ---------------------------------------------------------------------------

async def test_reopen_preserves_created_at(tmp_path: Path) -> None:
    conn1 = await open_main(11, tmp_path)
    created_first = await conn1.fetch_value(
        "SELECT value FROM meta WHERE key = 'created_at_ms'",
    )
    await conn1.close()

    # Yield event loop briefly so any wall-clock based assertion below would
    # actually have moved on if the seeder bumped created_at_ms.
    await asyncio.sleep(0.01)

    conn2 = await open_main(11, tmp_path)
    try:
        created_second = await conn2.fetch_value(
            "SELECT value FROM meta WHERE key = 'created_at_ms'",
        )
        assert created_first == created_second
    finally:
        await conn2.close()


# ---------------------------------------------------------------------------
# Raw DB
# ---------------------------------------------------------------------------

async def test_open_raw_creates_raw_tables(tmp_path: Path) -> None:
    conn = await open_raw(99, tmp_path)
    try:
        assert (tmp_path / f"99{RAW_DB_SUFFIX}").is_file()
        rows = await conn.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )
        names = {r["name"] for r in rows}
        assert names == {"meta", "raw_payload", "fetch_progress"}

        version = await conn.fetch_value(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        )
        assert int(version) == SUPPORTED_RAW_SCHEMA_VERSION
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Schema version mismatch
# ---------------------------------------------------------------------------

async def test_open_main_rejects_unknown_schema_version(tmp_path: Path) -> None:
    # Pre-create a DB with a poisoned schema_version so the open path's check trips.
    db_path = tmp_path / f"55{MAIN_DB_SUFFIX}"
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", "999"),
    )
    raw.commit()
    raw.close()

    with pytest.raises(SchemaMismatchError):
        await open_main(55, tmp_path)


# ---------------------------------------------------------------------------
# Transactional helpers
# ---------------------------------------------------------------------------

async def test_run_transaction_commits_atomically(main_conn: Connection) -> None:
    await main_conn.run_transaction([
        (
            "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
            "VALUES (?, ?, ?, ?)",
            ("BV1", "title-1", "{}", 1),
        ),
        (
            "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
            "VALUES (?, ?, ?, ?)",
            ("BV2", "title-2", "{}", 2),
        ),
    ])
    rows = await main_conn.fetch_all("SELECT bvid FROM video ORDER BY bvid")
    assert [r["bvid"] for r in rows] == ["BV1", "BV2"]


async def test_run_transaction_rolls_back_on_failure(
    main_conn: Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await main_conn.run_transaction([
            (
                "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
                "VALUES (?, ?, ?, ?)",
                ("BV_OK", "ok", "{}", 1),
            ),
            # Duplicate primary key — second statement raises, first must roll back.
            (
                "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
                "VALUES (?, ?, ?, ?)",
                ("BV_OK", "dup", "{}", 2),
            ),
        ])
    count = await main_conn.fetch_value("SELECT COUNT(*) FROM video")
    assert count == 0


async def test_execute_many_inserts_all_rows(main_conn: Connection) -> None:
    await main_conn.execute_many(
        "INSERT INTO video(bvid, title, payload, parsed_at_ms) VALUES (?, ?, ?, ?)",
        [
            ("BV_A", "a", "{}", 1),
            ("BV_B", "b", "{}", 2),
            ("BV_C", "c", "{}", 3),
        ],
    )
    count = await main_conn.fetch_value("SELECT COUNT(*) FROM video")
    assert count == 3
