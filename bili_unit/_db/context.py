# bili_unit._db.context — single-DB connection lifecycle for one uid.
#
# The unit writes one DB file per uid (raw.db). Stages bind to a
# UidContext to share that connection. Assembly opens the context once per
# uid and closes it on shutdown so we don't leak file handles when the host
# runs fetch / asr back-to-back for the same user.

from __future__ import annotations

import logging
from pathlib import Path

from .connection import Connection
from .paths import UidPaths, resolve

logger = logging.getLogger("bili.db.context")


class UidContext:
    """Holds the Connection for a single uid's raw DB.

    Stores receive a context (or its ``conn``) at construction; they don't open
    or close on their own. Callers (``BiliCommand``, tests) drive the lifecycle.
    """

    def __init__(self, uid: int, root: str | Path) -> None:
        self._paths = resolve(uid, root)
        self._conn: Connection | None = None

    @property
    def uid(self) -> int:
        return self._paths.uid

    @property
    def paths(self) -> UidPaths:
        return self._paths

    @property
    def conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError(f"UidContext({self.uid}) DB not opened")
        return self._conn

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        """Open the raw DB connection (idempotent)."""
        if self._conn is None:
            self._conn = Connection(self._paths.raw_db, uid=self.uid)
            await self._conn.open()

    async def close(self) -> None:
        """Close the connection (idempotent; best-effort)."""
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                logger.warning("uid_context_close_failed", extra={"uid": self.uid})
            self._conn = None


__all__ = ["UidContext"]
