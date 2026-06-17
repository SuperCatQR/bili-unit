# command — parsing write-side entry point.
#
# Per Phase 3 conventions: the command no longer holds a long-lived store or
# fetching query. Each call to ``parse_uid`` opens a :class:`UidContext`,
# constructs a :class:`ParsingStore` (writes) and a :class:`FetchingStore`
# (reads upstream raw payloads), runs the materializer, then closes the
# context.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._db import UidContext
from ..fetching._store import FetchingStore
from . import (
    ParsingCommandResult,
    ParsingModelStatus,
    ParsingTaskStatus,
)
from ._store import ParsingStore
from .materializer import ParsingMaterializer
from .specs import MODEL_ORDER

if TYPE_CHECKING:
    from .._env import BiliSettings

logger = logging.getLogger("bili.parsing.command")


class ParsingCommand:
    """Write-side entry for the parsing layer.

    This command only holds settings; the SQLite stores are constructed per
    ``parse_uid`` call and torn down on return.
    """

    def __init__(self, settings: BiliSettings) -> None:
        self._settings = settings

    async def parse_uid(
        self,
        uid: int,
        mode: str = "full",
        models: list[str] | None = None,
        download_images: bool = False,
    ) -> ParsingCommandResult:
        """Parse all raw payloads for a uid into typed objects.

        Args:
            uid: Target Bilibili user uid.
            mode: ``"full"`` re-parses everything; ``"incremental"`` skips
                items that already have a row in the main DB.
            models: optional explicit parsing model list. ``None`` means all
                registered models in ``MODEL_ORDER``.
            download_images: if True, downloads images for every parsed
                object after model parsing finishes.
        """
        if models is None:
            model_order = list(MODEL_ORDER)
        else:
            model_order = list(models)
            unknown = [name for name in model_order if name not in MODEL_ORDER]
            if unknown:
                raise ValueError(f"unknown parsing model(s): {', '.join(unknown)}")
            if not model_order:
                raise ValueError("models must not be empty")

        logger.info(
            "parse_uid_received",
            extra={"uid": uid, "mode": mode, "models": model_order},
        )

        ctx = UidContext(uid, self._settings.bili_db_dir)
        await ctx.open()
        try:
            parse_store = ParsingStore(ctx)
            fetch_store = FetchingStore(ctx)
            materializer = ParsingMaterializer(
                ctx=ctx,
                parse_store=parse_store,
                fetch_store=fetch_store,
            )

            # Initialise (or merge) the parsing stage_task row.
            await parse_store.init_task(model_order)
            await parse_store.update_task_status(ParsingTaskStatus.RUNNING.value)

            overall_status = ParsingTaskStatus.SUCCESS

            for model_name in model_order:
                try:
                    count = await materializer.parse_model(uid, model_name, mode)
                    await parse_store.update_task_model_status(
                        model_name,
                        ParsingModelStatus.SUCCESS.value,
                        count,
                    )
                    if count == 0 and not (
                        mode == "incremental"
                        and await self._model_has_existing_items(
                            parse_store, model_name,
                        )
                    ):
                        overall_status = ParsingTaskStatus.PARTIAL
                except Exception as exc:
                    logger.error(
                        "model_parse_failed",
                        extra={
                            "uid": uid,
                            "model": model_name,
                            "error": str(exc),
                        },
                    )
                    await parse_store.update_task_model_status(
                        model_name,
                        ParsingModelStatus.FAILED.value,
                    )
                    overall_status = ParsingTaskStatus.PARTIAL

            # Optional image-download step.
            if download_images:
                try:
                    images_summary = await materializer.download_images(uid)
                    await parse_store.update_task_images(images_summary)
                except Exception as exc:
                    logger.error(
                        "image_download_failed",
                        extra={"uid": uid, "error": str(exc)},
                    )

            # Finalise: persist the overall status. failed_item_ids is no
            # longer persisted — it's derived from the per-model statuses
            # (see ParsingTaskValue docstring).
            await parse_store.update_task_status(overall_status.value)

            return ParsingCommandResult(uid=uid, status=overall_status)
        finally:
            await ctx.close()

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """No-op: BiliCommand.delete_uid handles file IO directly.

        Kept on the API surface so callers that still iterate over per-stage
        ``delete_uid`` continue to work; the unit-level command does the
        actual cleanup of the two .db files plus the workdir.
        """
        return {}

    async def close(self) -> None:
        """No-op: stores are now per-call resources."""

    @staticmethod
    async def _model_has_existing_items(
        store: ParsingStore, model_name: str,
    ) -> bool:
        """Return True if ``model_name`` already has rows in the main DB."""
        return bool(await store.get_existing_item_ids(model_name))


__all__: list[str] = ["ParsingCommand"]
