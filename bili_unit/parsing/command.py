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
from ..observability import RunContext, RunReporter, RunStatus, SqliteSink
from . import (
    ParsingCommandResult,
    ParsingModelStatus,
    ParsingTaskStatus,
)
from ._store import ParsingStore
from .materializer import MissingRequiredRawPayloadError, ParsingMaterializer
from .specs import MODEL_ORDER

if TYPE_CHECKING:
    from .._env import BiliSettings

logger = logging.getLogger("bili.parsing.command")


def _run_status_from_task_status(status: ParsingTaskStatus) -> RunStatus:
    if status == ParsingTaskStatus.SUCCESS:
        return "SUCCESS"
    if status == ParsingTaskStatus.PARTIAL:
        return "PARTIAL"
    if status == ParsingTaskStatus.RUNNING:
        return "RUNNING"
    if status == ParsingTaskStatus.PENDING:
        return "PENDING"
    return "FAILED"


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
            reporter = RunReporter(
                RunContext.create(
                    uid=uid,
                    command="parse",
                    args={
                        "mode": mode,
                        "models": models,
                        "download_images": download_images,
                    },
                ),
                SqliteSink(ctx.main),
            )
            await reporter.start()
            await reporter.emit(
                "parse.run.started",
                stage="parsing",
                data={
                    "mode": mode,
                    "models": model_order,
                    "download_images": download_images,
                },
            )

            # Initialise (or merge) the parsing stage_task row.
            await parse_store.init_task(model_order)
            await parse_store.update_task_status(ParsingTaskStatus.RUNNING.value)

            overall_status = ParsingTaskStatus.SUCCESS

            for model_name in model_order:
                try:
                    await reporter.emit(
                        "parse.model.started",
                        stage="parsing",
                        item_type="model",
                        item_id=model_name,
                        data={"mode": mode},
                    )
                    count = await materializer.parse_model(uid, model_name, mode)
                    model_status = ParsingModelStatus.SUCCESS
                    await parse_store.update_task_model_status(
                        model_name,
                        model_status.value,
                        count,
                    )
                    await reporter.emit(
                        "parse.model.completed",
                        stage="parsing",
                        item_type="model",
                        item_id=model_name,
                        data={
                            "status": ParsingModelStatus.SUCCESS.value,
                            "count": count,
                        },
                    )
                except MissingRequiredRawPayloadError as exc:
                    logger.info(
                        "model_parse_skipped_missing_raw",
                        extra={
                            "uid": uid,
                            "model": model_name,
                            "missing_endpoints": list(exc.missing_endpoints),
                        },
                    )
                    await parse_store.update_task_model_status(
                        model_name,
                        ParsingModelStatus.SKIPPED.value,
                        0,
                    )
                    await reporter.emit(
                        "parse.model.skipped",
                        stage="parsing",
                        level="WARNING",
                        item_type="model",
                        item_id=model_name,
                        message=str(exc),
                        data={
                            "reason": "missing_required_raw_payload",
                            "missing_endpoints": list(exc.missing_endpoints),
                        },
                    )
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
                    await reporter.emit(
                        "parse.model.failed",
                        stage="parsing",
                        level="ERROR",
                        item_type="model",
                        item_id=model_name,
                        message=str(exc),
                        data={"error_type": type(exc).__name__},
                    )
                    overall_status = ParsingTaskStatus.PARTIAL

            # Optional image-download step.
            if download_images:
                try:
                    await reporter.emit(
                        "parse.images.started",
                        stage="parsing",
                    )
                    images_summary = await materializer.download_images(uid)
                    await parse_store.update_task_images(images_summary)
                    await reporter.emit(
                        "parse.images.completed",
                        stage="parsing",
                        data=images_summary,
                    )
                except Exception as exc:
                    logger.error(
                        "image_download_failed",
                        extra={"uid": uid, "error": str(exc)},
                    )
                    await reporter.emit(
                        "parse.images.failed",
                        stage="parsing",
                        level="ERROR",
                        message=str(exc),
                        data={"error_type": type(exc).__name__},
                    )

            # Finalise: persist the overall status. failed_item_ids is no
            # longer persisted — it's derived from the per-model statuses
            # (see ParsingTaskValue docstring).
            await parse_store.update_task_status(overall_status.value)

            summary = {"status": overall_status.value, "models": model_order}
            await reporter.emit(
                "parse.run.completed",
                stage="parsing",
                data=summary,
            )
            await reporter.complete(
                _run_status_from_task_status(overall_status),
                summary=summary,
            )

            return ParsingCommandResult(
                uid=uid,
                status=overall_status,
                run_id=reporter.context.run_id,
            )
        except Exception as exc:
            if "reporter" in locals():
                await reporter.emit(
                    "parse.run.failed",
                    stage="parsing",
                    level="ERROR",
                    data={"error_type": type(exc).__name__, "error": str(exc)},
                )
                await reporter.complete(
                    "FAILED",
                    summary={
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise
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
