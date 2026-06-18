# bili_unit._db.connection — async wrapper over sqlite3.
#
# Why not aiosqlite: project already uses asyncio.to_thread for blocking I/O
# (see bili_unit/_storage/_kv.py); aiosqlite is a thin to_thread wrapper.
# Skipping the dependency keeps the supply chain smaller.
#
# Concurrency model:
#   * One Connection instance per (uid, kind) — main and raw are separate.
#   * Connection wraps a sqlite3.Connection in an asyncio.Lock so multiple
#     awaiting writers from the same event loop can't interleave a transaction.
#   * Cross-uid concurrency is fine: each uid has its own files.
#
# Schema versioning:
#   Open path runs apply_ddl() unconditionally (CREATE IF NOT EXISTS, idempotent).
#   After DDL, meta.schema_version is read; mismatch raises SchemaMismatchError.

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from .ddl import read_ddl

logger = logging.getLogger("bili.db.connection")

# Single source of truth for the *currently expected* schema. Bump together
# with a new ddl/main_v{N}.sql when we ever do a real migration.
SUPPORTED_MAIN_SCHEMA_VERSION = 2
SUPPORTED_RAW_SCHEMA_VERSION = 1

DbKind = Literal["main", "raw"]


class DbError(Exception):
    """Base for DB-layer exceptions raised by Connection."""


class SchemaMismatchError(DbError):
    """Raised when an existing DB's schema_version is not what this code expects."""


class Connection:
    """Thin async wrapper around a single sqlite3 Connection.

    Holds the connection + an asyncio.Lock. All public methods are async;
    blocking sqlite3 work runs via :func:`asyncio.to_thread` so the event loop
    stays responsive (matches the existing JsonKVStore pattern).
    """

    def __init__(
        self,
        path: Path,
        *,
        kind: DbKind,
        uid: int,
    ) -> None:
        self._path = path
        self._kind = kind
        self._uid = uid
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def kind(self) -> DbKind:
        return self._kind

    @property
    def uid(self) -> int:
        return self._uid

    @property
    def lock(self) -> asyncio.Lock:
        """Public for stores that need to bundle multi-statement work atomically."""
        return self._lock

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        """Open the connection, run DDL, verify schema_version."""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open_sync)
        await asyncio.to_thread(self._apply_ddl_and_seed)

    def _open_sync(self) -> sqlite3.Connection:
        # isolation_level=None puts us in autocommit mode; we manage transactions
        # explicitly via BEGIN / COMMIT in execute_many / transaction().
        conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # Pragmas that must run on every open (DDL also sets WAL but it's a
        # one-time persistent setting; foreign_keys is per-connection).
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _apply_ddl_and_seed(self) -> None:
        assert self._conn is not None
        ddl_name = (
            f"main_v{SUPPORTED_MAIN_SCHEMA_VERSION}"
            if self._kind == "main"
            else f"raw_v{SUPPORTED_RAW_SCHEMA_VERSION}"
        )
        ddl_sql = read_ddl(ddl_name)
        # executescript handles multi-statement DDL but auto-commits any
        # pending tx; safe here because we just opened the connection.
        self._conn.executescript(ddl_sql)
        self._seed_meta()
        self._verify_schema_version()

    def _seed_meta(self) -> None:
        """Insert (uid, schema_version, created_at_ms) on first open; idempotent."""
        assert self._conn is not None
        import time

        now_ms = int(time.time() * 1000)
        version = (
            SUPPORTED_MAIN_SCHEMA_VERSION
            if self._kind == "main"
            else SUPPORTED_RAW_SCHEMA_VERSION
        )
        # INSERT OR IGNORE so re-opens don't bump created_at_ms.
        self._conn.executemany(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(version)),
                ("uid", str(self._uid)),
                ("created_at_ms", str(now_ms)),
            ],
        )

    def _verify_schema_version(self) -> None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
        if row is None:
            raise SchemaMismatchError(
                f"{self._path}: meta.schema_version missing after DDL apply",
            )
        stored = int(row[0])
        expected = (
            SUPPORTED_MAIN_SCHEMA_VERSION
            if self._kind == "main"
            else SUPPORTED_RAW_SCHEMA_VERSION
        )
        if stored != expected:
            raise SchemaMismatchError(
                f"{self._path}: schema_version={stored}, "
                f"this build supports {expected}. "
                f"Migration tooling for v{stored}→v{expected} is not implemented.",
            )

    async def close(self) -> None:
        if self._conn is None:
            return
        await asyncio.to_thread(self._conn.close)
        self._conn = None

    # -- queries -----------------------------------------------------------

    async def execute(
        self, sql: str, params: Sequence[Any] = (),
    ) -> None:
        """Run a single statement that returns no rows. Held under the lock."""
        async with self._lock:
            await asyncio.to_thread(self._exec_sync, sql, params)

    async def execute_many(
        self, sql: str, seq_of_params: Iterable[Sequence[Any]],
    ) -> None:
        params_list = list(seq_of_params)
        if not params_list:
            return
        async with self._lock:
            await asyncio.to_thread(self._exec_many_sync, sql, params_list)

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_one_sync, sql, params)

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    async def fetch_value(
        self, sql: str, params: Sequence[Any] = (),
    ) -> Any:
        """Return the first column of the first row, or None."""
        row = await self.fetch_one(sql, params)
        return None if row is None else row[0]

    # -- sync workers (called from to_thread) ------------------------------

    def _exec_sync(self, sql: str, params: Sequence[Any]) -> None:
        assert self._conn is not None
        self._conn.execute(sql, params)

    def _exec_many_sync(
        self, sql: str, seq_of_params: list[Sequence[Any]],
    ) -> None:
        assert self._conn is not None
        # Wrap in an explicit transaction so the executemany is atomic.
        try:
            self._conn.execute("BEGIN")
            self._conn.executemany(sql, seq_of_params)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _fetch_one_sync(
        self, sql: str, params: Sequence[Any],
    ) -> sqlite3.Row | None:
        assert self._conn is not None
        return self._conn.execute(sql, params).fetchone()

    def _fetch_all_sync(
        self, sql: str, params: Sequence[Any],
    ) -> list[sqlite3.Row]:
        assert self._conn is not None
        return self._conn.execute(sql, params).fetchall()

    # -- multi-statement transaction ---------------------------------------

    async def run_transaction(
        self, statements: list[tuple[str, Sequence[Any]]],
    ) -> None:
        """Run several statements inside one BEGIN/COMMIT under the connection lock.

        The store layer uses this for writes that must commit together (e.g.
        raw_payload + fetch_progress, the spiritual successor to
        ``write_pair_locked``).
        """
        if not statements:
            return
        async with self._lock:
            await asyncio.to_thread(self._run_tx_sync, statements)

    def _run_tx_sync(
        self, statements: list[tuple[str, Sequence[Any]]],
    ) -> None:
        assert self._conn is not None
        try:
            self._conn.execute("BEGIN")
            for sql, params in statements:
                self._conn.execute(sql, params)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise


__all__ = [
    "SUPPORTED_MAIN_SCHEMA_VERSION",
    "SUPPORTED_RAW_SCHEMA_VERSION",
    "Connection",
    "DbError",
    "DbKind",
    "SchemaMismatchError",
]
