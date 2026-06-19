# bili_unit._db — SQLite persistence layer.
#
# The unit writes one DB file per uid (``output/bili/{uid}.raw.db``). See
# docs/schema.md for the consumer contract.
#
# Public surface:
#
#   * UidPaths / resolve / list_uids — uid → on-disk path mapping
#   * Connection                     — async wrapper over the raw DB file
#   * UidContext                     — connection lifecycle for one uid
#   * open_db                        — convenience: resolve + Connection + open
#   * SchemaMismatchError, DbError   — exception hierarchy

from __future__ import annotations

from pathlib import Path

from .connection import (
    SUPPORTED_SCHEMA_VERSION,
    Connection,
    DbError,
    SchemaMismatchError,
)
from .context import UidContext
from .paths import (
    RAW_DB_SUFFIX,
    UidPaths,
    list_uids,
    resolve,
)


async def open_db(uid: int, root: str | Path) -> Connection:
    """Open (creating if needed) the raw DB for ``uid`` and apply DDL."""
    paths = resolve(uid, root)
    conn = Connection(paths.raw_db, uid=uid)
    await conn.open()
    return conn


__all__ = [
    "RAW_DB_SUFFIX",
    "SUPPORTED_SCHEMA_VERSION",
    "Connection",
    "DbError",
    "SchemaMismatchError",
    "UidContext",
    "UidPaths",
    "list_uids",
    "open_db",
    "resolve",
]
