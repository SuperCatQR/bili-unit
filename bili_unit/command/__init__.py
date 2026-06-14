# bili_unit.command — unified write-side entry across stages.
#
# Per docs/structure/bili.md §10, the bili unit's outward write entry is the
# `command/` package. Its job is to编排 fetching / parsing / processing /
# cleansing stages (each owns its own internal command). Today fetching +
# parsing + processing are wired; cleansing comes later.
#
# Boundaries (docs/structure/bili.md §8):
#   - command 不直接调用 client / transform / audio
#   - command 不写 raw / temp / data
#   - command 不提供 data / error 读取（那是 query 的事）

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._manifest import compute_manifest, delete_manifest, write_manifest
from ..fetching import CommandResult
from ..fetching.command import Command as _FetchingCommand

if TYPE_CHECKING:
    from .._env import BiliSettings
    from ..parsing import ParsingCommandResult
    from ..parsing.command import ParsingCommand as _ParsingCommand
    from ..processing import ProcessingCommandResult
    from ..processing.command import ProcessingCommand as _ProcessingCommand
    from ..query import BiliQuery

logger = logging.getLogger("bili.command")


class BiliCommand:
    """Bili unit的统一写侧入口；编排各阶段 command。"""

    def __init__(
        self,
        fetching: _FetchingCommand,
        parsing: _ParsingCommand | None = None,
        processing: _ProcessingCommand | None = None,
        *,
        query: BiliQuery | None = None,
        settings: BiliSettings | None = None,
    ) -> None:
        self._fetching = fetching
        self._parsing = parsing
        self._processing = processing
        # ``query`` and ``settings`` are required for manifest persistence;
        # callers that omit them get the historical no-manifest behaviour
        # (kept for backward compatibility with embeddings + tests).
        self._query = query
        self._settings = settings

    # -- fetching stage ----------------------------------------------------

    async def fetch(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        """触发 fetching 抓取流水线。"""
        result = await self._fetching.fetch_uid(uid, endpoints, mode=mode)
        await self._persist_manifest(uid)
        return result

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
        result = await self._parsing.parse_uid(
            uid, mode=mode, download_images=download_images,
        )
        await self._persist_manifest(uid)
        return result

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
        result = await self._processing.process_uid(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
        )
        await self._persist_manifest(uid)
        return result

    # -- delete uid (cross-stage) ------------------------------------------

    async def delete_uid(self, uid: int) -> dict[str, dict[str, int]]:
        """Delete all state for a uid across every assembled stage.

        Executes in pipeline order (fetching → parsing → processing). If any
        stage raises, later stages are skipped and the exception propagates;
        the partial deletion is left as-is. Re-running ``delete_uid`` after
        fixing the underlying issue is idempotent and will clear whatever
        remains.
        """
        if self._parsing is None:
            raise RuntimeError("parsing command was not assembled")
        if self._processing is None:
            raise RuntimeError("processing command was not assembled")
        fetching_stats = await self._fetching.delete_uid(uid)
        parsing_stats = await self._parsing.delete_uid(uid)
        processing_stats = await self._processing.delete_uid(uid)
        if self._settings is not None:
            try:
                delete_manifest(uid, self._settings.bili_manifest_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "manifest_delete_failed",
                    extra={"uid": uid, "error": str(exc)},
                )
        return {
            "fetching": fetching_stats,
            "parsing": parsing_stats,
            "processing": processing_stats,
        }

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        if self._processing is not None:
            await self._processing.close()
        if self._parsing is not None:
            await self._parsing.close()
        await self._fetching.close()

    # -- internal ----------------------------------------------------------

    async def _persist_manifest(self, uid: int) -> None:
        """Recompute + write the per-uid manifest after a stage run.

        No-op when the command was assembled without a query / settings (the
        backward-compat path for narrow tests). Failures are logged but never
        propagated — the manifest is best-effort and must not break the
        primary stage flow.
        """
        if self._query is None or self._settings is None:
            return
        try:
            manifest = await compute_manifest(uid, self._query)
            write_manifest(uid, self._settings.bili_manifest_dir, manifest)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "manifest_persist_failed",
                extra={"uid": uid, "error": str(exc)},
            )


__all__ = ["BiliCommand"]
