# bili_unit.command — unified write-side entry across stages.
#
# Per docs/structure/bili.md §10, the bili unit's outward write entry is the
# `command/` package. Its job is to编排 fetching / parsing / processing /
# cleansing stages (each owns its own internal command).
#
# Boundaries:
#   - command 不直接调用 client / transform / audio
#   - command 不写 raw / temp / data
#   - command 不提供 data / error 读取（消费者直接 sqlite3 查 db_path(uid)）

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .._db.paths import resolve as _resolve_paths
from ..fetching import CommandResult
from ..fetching.command import Command as _FetchingCommand

if TYPE_CHECKING:
    from .._env import BiliSettings
    from ..parsing import ParsingCommandResult
    from ..parsing.command import ParsingCommand as _ParsingCommand
    from ..processing import ProcessingCommandResult
    from ..processing.command import ProcessingCommand as _ProcessingCommand

logger = logging.getLogger("bili.command")


class BiliCommand:
    """Bili unit的统一写侧入口；编排各阶段 command。

    Phase 3 simplification (vs. legacy):
      * No ``query`` parameter — read side is consumer's SQL, not a Python facade.
      * No ``_persist_manifest`` — manifest is now ``manifest_summary`` SQL VIEW.
      * ``delete_uid`` deletes the on-disk files directly (one main DB, one raw
        DB, one workdir per uid) instead of routing through per-stage stores.
    """

    def __init__(
        self,
        fetching: _FetchingCommand,
        parsing: _ParsingCommand | None = None,
        processing: _ProcessingCommand | None = None,
        *,
        settings: BiliSettings | None = None,
    ) -> None:
        self._fetching = fetching
        self._parsing = parsing
        self._processing = processing
        self._settings = settings

    # -- fetching stage ----------------------------------------------------

    async def fetch(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        """触发 fetching 抓取流水线。"""
        return await self._fetching.fetch_uid(uid, endpoints, mode=mode)

    # -- parsing stage -----------------------------------------------------

    async def parse(
        self,
        uid: int,
        mode: str = "full",
        download_images: bool = False,
    ) -> ParsingCommandResult:
        """触发 parsing 解析流水线。"""
        if self._parsing is None:
            raise RuntimeError("parsing command was not assembled")
        return await self._parsing.parse_uid(
            uid, mode=mode, download_images=download_images,
        )

    # -- processing stage --------------------------------------------------

    async def process(
        self,
        uid: int,
        mode: str = "incremental",
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
    ) -> ProcessingCommandResult:
        """触发 processing 处理流水线。"""
        if self._processing is None:
            raise RuntimeError("processing command was not assembled")
        return await self._processing.process_uid(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
        )

    # -- delete uid (file IO; no per-stage routing) ------------------------

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """Delete every on-disk artefact for one uid.

        Removes:
          * ``{db_dir}/{uid}.db``      (main DB)
          * ``{db_dir}/{uid}.raw.db``  (raw DB)
          * ``{db_dir}/{uid}/``        (workdir: images / audio caches)

        Returns ``{"main_db": 0|1, "raw_db": 0|1, "workdir_files": N}``.
        Idempotent — missing files yield 0.
        """
        if self._settings is None:
            raise RuntimeError(
                "delete_uid requires settings; pass settings= when constructing BiliCommand",
            )
        paths = _resolve_paths(uid, self._settings.bili_db_dir)
        stats = {"main_db": 0, "raw_db": 0, "workdir_files": 0}
        if paths.main_db.exists():
            paths.main_db.unlink()
            stats["main_db"] = 1
            # SQLite WAL companion files (-wal / -shm) — also remove.
            for ext in ("-wal", "-shm"):
                companion = Path(str(paths.main_db) + ext)
                if companion.exists():
                    companion.unlink()
        if paths.raw_db.exists():
            paths.raw_db.unlink()
            stats["raw_db"] = 1
            for ext in ("-wal", "-shm"):
                companion = Path(str(paths.raw_db) + ext)
                if companion.exists():
                    companion.unlink()
        if paths.workdir.exists():
            # Count files for stats (recursive) before nuking the tree.
            stats["workdir_files"] = sum(
                1 for _ in paths.workdir.rglob("*") if _.is_file()
            )
            shutil.rmtree(paths.workdir)
        return stats

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        if self._processing is not None:
            await self._processing.close()
        if self._parsing is not None:
            await self._parsing.close()
        await self._fetching.close()


__all__ = ["BiliCommand"]
