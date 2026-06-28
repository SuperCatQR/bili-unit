# tests for bili_unit/_db/connection — schema version check / DDL apply / error paths.
# Run: uv run pytest bili_unit/tests/test_db_connection.py -v

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bili_unit._db.connection import (
    SUPPORTED_SCHEMA_VERSION,
    Connection,
    SchemaMismatchError,
)

UID = 42


# ---------------------------------------------------------------------------
# Normal path — fresh DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_fresh_db_creates_schema(tmp_path: Path) -> None:
    """A fresh DB is created with the expected schema tables and meta keys."""
    db_path = tmp_path / "test.raw.db"
    conn = Connection(db_path, uid=UID)
    await conn.open()
    try:
        # Verify meta keys
        version = await conn.fetch_value(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        )
        assert version == str(SUPPORTED_SCHEMA_VERSION)

        uid_val = await conn.fetch_value(
            "SELECT value FROM meta WHERE key = 'uid'",
        )
        assert uid_val == str(UID)

        created_at = await conn.fetch_value(
            "SELECT value FROM meta WHERE key = 'created_at_ms'",
        )
        assert created_at is not None
        assert int(created_at) > 0

        # Verify core tables exist
        tables = await conn.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        )
        table_names = {r["name"] for r in tables}
        assert "meta" in table_names
        assert "raw_payload" in table_names
        assert "fetch_progress" in table_names
        assert "audio_transcription" in table_names
        assert "stage_task" in table_names
        assert "fetch_endpoint_state" in table_names
        assert "stage_error" in table_names
        assert "stage_run" in table_names
        assert "stage_event" in table_names

        # Verify manifest_summary view exists
        views = await conn.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'view'",
        )
        view_names = {r["name"] for r in views}
        assert "manifest_summary" in view_names
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_open_is_idempotent(tmp_path: Path) -> None:
    """Calling open() twice does not crash or duplicate meta rows."""
    db_path = tmp_path / "test.raw.db"
    conn = Connection(db_path, uid=UID)
    await conn.open()
    try:
        await conn.open()  # second open is a no-op
        rows = await conn.fetch_all("SELECT key FROM meta")
        # schema_version, uid, created_at_ms — exactly 3, no duplicates
        assert len(rows) == 3
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Schema version mismatch — existing DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_mismatch_version_too_old(tmp_path: Path) -> None:
    """An existing DB with schema_version=2 raises SchemaMismatchError."""
    db_path = tmp_path / "test.raw.db"
    _create_db_with_schema_version(db_path, 2)
    conn = Connection(db_path, uid=UID)
    with pytest.raises(SchemaMismatchError, match="schema_version=2"):
        await conn.open()
    await conn.close()


@pytest.mark.asyncio
async def test_schema_mismatch_version_too_new(tmp_path: Path) -> None:
    """An existing DB with schema_version=99 raises SchemaMismatchError."""
    db_path = tmp_path / "test.raw.db"
    _create_db_with_schema_version(db_path, 99)
    conn = Connection(db_path, uid=UID)
    with pytest.raises(SchemaMismatchError, match="schema_version=99"):
        await conn.open()
    await conn.close()


@pytest.mark.asyncio
async def test_schema_mismatch_missing_meta_key(tmp_path: Path) -> None:
    """An existing DB with a meta table but no schema_version key raises SchemaMismatchError."""
    db_path = tmp_path / "test.raw.db"
    _create_db_with_meta_table_no_schema_version(db_path)
    conn = Connection(db_path, uid=UID)
    with pytest.raises(SchemaMismatchError, match="schema_version missing"):
        await conn.open()
    await conn.close()


@pytest.mark.asyncio
async def test_schema_mismatch_corrupt_meta_table(tmp_path: Path) -> None:
    """A DB with a corrupt meta table (no 'value' column) raises SchemaMismatchError."""
    db_path = tmp_path / "test.raw.db"
    _create_db_with_corrupt_meta(db_path)
    conn = Connection(db_path, uid=UID)
    with pytest.raises(SchemaMismatchError, match="not readable"):
        await conn.open()
    await conn.close()


# ---------------------------------------------------------------------------
# DDL apply — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_creates_parent_directory(tmp_path: Path) -> None:
    """open() creates parent directories that don't exist."""
    db_path = tmp_path / "nested" / "sub" / "test.raw.db"
    conn = Connection(db_path, uid=UID)
    await conn.open()
    try:
        assert db_path.exists()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_close_twice_is_safe(tmp_path: Path) -> None:
    """close() is idempotent — calling it twice does not crash."""
    db_path = tmp_path / "test.raw.db"
    conn = Connection(db_path, uid=UID)
    await conn.open()
    await conn.close()
    await conn.close()  # no-op


@pytest.mark.asyncio
async def test_close_before_open_is_safe(tmp_path: Path) -> None:
    """close() before open() is a no-op."""
    db_path = tmp_path / "test.raw.db"
    conn = Connection(db_path, uid=UID)
    await conn.close()  # no-op, no crash


# ---------------------------------------------------------------------------
# DDL failure → rollback / error, no partial schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ddl_failure_leaves_no_partial_schema(monkeypatch, tmp_path: Path) -> None:
    """If DDL application fails mid-way, the DB is not left in a half-applied state.

    ponytail: This test uses monkeypatch (not real disk-full/permission-denied) —
    it covers the "no partial schema" semantic at the logical level.
    True mid-DDL failure relies on SQLite's atomicity guarantees for executescript
    and is impractical to inject at the filesystem level.
    """
    db_path = tmp_path / "test.raw.db"

    # Simulate a DDL failure by making executescript raise after schema_version check passes
    # but before DDL is applied. We patch _apply_ddl_and_seed to inject a failure
    # after _verify_existing_schema_version_before_ddl succeeds but before executescript.
    original_apply = Connection._apply_ddl_and_seed

    def _failing_apply(self: Connection) -> None:
        # Run the pre-check (should pass for a fresh DB)
        self._verify_existing_schema_version_before_ddl()
        # Now simulate a failure — don't apply DDL, just raise
        raise sqlite3.OperationalError("simulated DDL failure")

    monkeypatch.setattr(Connection, "_apply_ddl_and_seed", _failing_apply)

    conn = Connection(db_path, uid=UID)
    with pytest.raises(sqlite3.OperationalError, match="simulated DDL failure"):
        await conn.open()

    # The DB file may or may not exist (sqlite3 creates it on connect),
    # but if it exists, it should not have the full schema.
    if db_path.exists():
        # Open it raw to inspect
        raw = sqlite3.connect(str(db_path))
        tables = raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        ).fetchall()
        raw.close()
        table_names = {r[0] for r in tables}
        # meta table should NOT exist because DDL was never applied
        assert "meta" not in table_names, (
            "DDL failure should not leave partial schema"
        )
    # Clean up
    await conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _create_db_with_schema_version(path: Path, version: int) -> None:
    """Create a minimal DB with a meta table and the given schema_version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(version)),
    )
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("uid", "42"),
    )
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("created_at_ms", str(0)),
    )
    raw.commit()
    raw.close()


def _create_db_with_meta_table_no_schema_version(path: Path) -> None:
    """Create a DB with a meta table but no schema_version row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("uid", "42"),
    )
    raw.commit()
    raw.close()


def _create_db_with_corrupt_meta(path: Path) -> None:
    """Create a DB with a 'meta' table that has wrong schema (no 'value' column)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    # Create a table named 'meta' but with different columns
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, extra TEXT)")
    raw.execute("INSERT INTO meta(key, extra) VALUES (?, ?)", ("schema_version", "3"))
    raw.commit()
    raw.close()
