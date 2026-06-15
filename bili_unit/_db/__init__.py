# bili_unit._db — SQLite persistence layer.
#
# Replaces the old bili_unit._storage (file-directory JSON KV store).
# See docs/refactor-plan-sqlite.md for the full transition plan.
#
# Public surface:
#
#   * UidPaths / resolve / list_uids — uid → on-disk path mapping
#   * Connection                     — async wrapper over a single sqlite3 file
#   * open_main / open_raw           — convenience: resolve + Connection + open
#   * SchemaMismatchError, DbError   — exception hierarchy
#
# Stage stores (FetchingStore, ParsingStore, ProcessingStore) live in their
# stage packages and use Connection underneath; this layer is stage-agnostic.

from __future__ import annotations

from pathlib import Path

from .connection import (
    SUPPORTED_MAIN_SCHEMA_VERSION,
    SUPPORTED_RAW_SCHEMA_VERSION,
    Connection,
    DbError,
    DbKind,
    SchemaMismatchError,
)
from .context import UidContext
from .paths import (
    MAIN_DB_SUFFIX,
    RAW_DB_SUFFIX,
    UidPaths,
    list_uids,
    resolve,
)


async def open_main(uid: int, root: str | Path) -> Connection:
    """Open (creating if needed) the main DB for ``uid`` and apply DDL."""
    paths = resolve(uid, root)
    conn = Connection(paths.main_db, kind="main", uid=uid)
    await conn.open()
    return conn


async def open_raw(uid: int, root: str | Path) -> Connection:
    """Open (creating if needed) the raw DB for ``uid`` and apply DDL."""
    paths = resolve(uid, root)
    conn = Connection(paths.raw_db, kind="raw", uid=uid)
    await conn.open()
    return conn


__all__ = [
    "MAIN_DB_SUFFIX",
    "RAW_DB_SUFFIX",
    "SUPPORTED_MAIN_SCHEMA_VERSION",
    "SUPPORTED_RAW_SCHEMA_VERSION",
    "Connection",
    "DbError",
    "DbKind",
    "SchemaMismatchError",
    "UidContext",
    "UidPaths",
    "list_uids",
    "open_main",
    "open_raw",
    "resolve",
]
