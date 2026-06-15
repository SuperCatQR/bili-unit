# bili_unit._db.context — paired (main, raw) connection lifecycle for one uid.
#
# Every stage store binds to a UidContext: parsing and processing only use
# main; fetching writes both. Assembly opens the context once per uid and
# closes it on shutdown so we don't leak file handles when the host runs
# fetch/parse/process back-to-back for the same user.

from __future__ import annotations

import logging
from pathlib import Path

from .connection import Connection
from .paths import UidPaths, resolve

logger = logging.getLogger("bili.db.context")


class UidContext:
    """Holds the (main, raw) Connection pair for a single uid.

    Stores receive a context (or just its ``main`` / ``raw`` connections) at
    construction; they don't open or close on their own. This mirrors the old
    pattern where ``DataStore.open()`` was called once by ``assemble()`` and
    every command shared it.
    """

    def __init__(self, uid: int, root: str | Path) -> None:
        self._paths = resolve(uid, root)
        self._main: Connection | None = None
        self._raw: Connection | None = None

    @property
    def uid(self) -> int:
        return self._paths.uid

    @property
    def paths(self) -> UidPaths:
        return self._paths

    @property
    def main(self) -> Connection:
        if self._main is None:
            raise RuntimeError(f"UidContext({self.uid}) main DB not opened")
        return self._main

    @property
    def raw(self) -> Connection:
        if self._raw is None:
            raise RuntimeError(f"UidContext({self.uid}) raw DB not opened")
        return self._raw

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        """Open both DBs (idempotent)."""
        if self._main is None:
            self._main = Connection(
                self._paths.main_db, kind="main", uid=self.uid,
            )
            await self._main.open()
        if self._raw is None:
            self._raw = Connection(
                self._paths.raw_db, kind="raw", uid=self.uid,
            )
            await self._raw.open()

    async def close(self) -> None:
        """Close both DBs (idempotent; best-effort on each)."""
        for attr in ("_main", "_raw"):
            conn = getattr(self, attr)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    logger.warning("uid_context_close_failed", extra={"uid": self.uid})
                setattr(self, attr, None)


__all__ = ["UidContext"]
