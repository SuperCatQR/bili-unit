"""Unified write-side command for fetching, parsing, sync, ASR, and delete."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .._db.paths import resolve as _resolve_paths
from ..fetching import CommandResult
from ..fetching.command import Command as _FetchingCommand
from ._workflow import SyncCommandResult, sync_uid

if TYPE_CHECKING:
    from .._env import BiliSettings
    from ..parsing import ParsingCommandResult
    from ..parsing.command import ParsingCommand as _ParsingCommand
    from ..processing import ProcessingCommandResult
    from ..processing.command import ProcessingCommand as _ProcessingCommand

logger = logging.getLogger("bili.command")


class BiliCommand:
    """CLI-first write entry for all stages."""

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

    async def fetch(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        """Run fetching for one uid."""
        return await self._fetching.fetch_uid(uid, endpoints, mode=mode)

    async def parse(
        self,
        uid: int,
        mode: str = "full",
        download_images: bool = False,
    ) -> ParsingCommandResult:
        """Run parsing for one uid."""
        if self._parsing is None:
            raise RuntimeError("parsing command was not assembled")
        return await self._parsing.parse_uid(
            uid, mode=mode, download_images=download_images,
        )

    async def sync(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        *,
        fetch_mode: str = "incremental",
        parse_mode: str = "full",
        download_images: bool = False,
    ) -> SyncCommandResult:
        """Run fetching followed by parsing."""
        return await sync_uid(
            self,
            uid,
            endpoints=endpoints,
            fetch_mode=fetch_mode,
            parse_mode=parse_mode,
            download_images=download_images,
        )

    async def asr(
        self,
        uid: int,
        mode: str = "incremental",
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> ProcessingCommandResult:
        """Run the ASR pipeline for one uid."""
        if self._processing is None:
            raise RuntimeError("ASR command was not assembled")
        return await self._processing.asr_uid(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
            max_audio_seconds=max_audio_seconds,
            max_audio_tokens=max_audio_tokens,
        )

    async def process(
        self,
        uid: int,
        mode: str = "incremental",
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> ProcessingCommandResult:
        """Backward-compatible alias for :meth:`asr`."""
        return await self.asr(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
            max_audio_seconds=max_audio_seconds,
            max_audio_tokens=max_audio_tokens,
        )

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """Delete the main DB, raw DB, and workdir for one uid."""
        if self._settings is None:
            raise RuntimeError(
                "delete_uid requires settings; pass settings= when constructing BiliCommand",
            )
        paths = _resolve_paths(uid, self._settings.bili_db_dir)
        stats = {"main_db": 0, "raw_db": 0, "workdir_files": 0}
        if paths.main_db.exists():
            paths.main_db.unlink()
            stats["main_db"] = 1
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
            stats["workdir_files"] = sum(
                1 for path in paths.workdir.rglob("*") if path.is_file()
            )
            shutil.rmtree(paths.workdir)
        return stats

    async def close(self) -> None:
        if self._processing is not None:
            await self._processing.close()
        if self._parsing is not None:
            await self._parsing.close()
        await self._fetching.close()


__all__ = ["BiliCommand", "SyncCommandResult"]
