# command — parsing write-side entry point.
#
# Delegates fetch-to-store orchestration to ParsingMaterializer, while this
# module owns task status and the write-side command Interface.

from __future__ import annotations

import logging
import shutil
import time
from typing import TYPE_CHECKING, Any

from . import (
    ParsingCommandResult,
    ParsingModelStatus,
    ParsingTaskStatus,
    ParsingTaskValue,
)
from .data import ParsingDataStore
from .keys import _task_key
from .materializer import ParsingMaterializer
from .specs import MODEL_ORDER

if TYPE_CHECKING:
    from ..fetching.protocols import FetchingReadView

logger = logging.getLogger("bili.parsing.command")

class ParsingCommand:
    """Write-side entry for the parsing layer.

    Reads from fetching.query, calls model parsers, writes to parsing.data.
    """

    def __init__(
        self,
        data: ParsingDataStore,
        fetching_query: FetchingReadView,
    ) -> None:
        self._data = data
        self._fetch_qry = fetching_query
        self._materializer = ParsingMaterializer(data, fetching_query)

    async def parse_uid(
        self,
        uid: int,
        mode: str = "full",
        download_images: bool = False,
    ) -> ParsingCommandResult:
        """Parse all raw payloads for a uid into typed objects.

        Args:
            uid: Target Bilibili user uid.
            mode: "full" re-parses everything; "incremental" skips already-parsed items.
            download_images: If True, download images after parsing.
        """
        logger.info("parse_uid_received", extra={"uid": uid, "mode": mode})

        task_key = _task_key(uid)
        now = int(time.time() * 1000)

        # Initialize task
        task_val = ParsingTaskValue(uid=uid, status=ParsingTaskStatus.RUNNING, created_at=now)
        await self._data.put(task_key, task_val.to_dict())

        overall_status = ParsingTaskStatus.SUCCESS

        for model_name in MODEL_ORDER:
            try:
                count = await self._parse_model(uid, model_name, mode)
                await self._data.update_task_model_status(
                    task_key, model_name,
                    ParsingModelStatus.SUCCESS.value,
                    count,
                )
                if count == 0 and not (
                    mode == "incremental"
                    and await self._model_has_existing_items(uid, model_name)
                ):
                    overall_status = ParsingTaskStatus.PARTIAL
            except Exception as exc:
                logger.error(
                    "model_parse_failed",
                    extra={"uid": uid, "model": model_name, "error": str(exc)},
                )
                await self._data.update_task_model_status(
                    task_key, model_name,
                    ParsingModelStatus.FAILED.value,
                )
                overall_status = ParsingTaskStatus.PARTIAL

        # Optional image download step
        if download_images:
            try:
                images_summary = await self._download_images(uid)
                await self._data.update_task_images(task_key, images_summary)
            except Exception as exc:
                logger.error(
                    "image_download_failed",
                    extra={"uid": uid, "error": str(exc)},
                )

        # Finalize task
        task_d = await self._data.get(task_key)
        if task_d is not None:
            tv = ParsingTaskValue.from_dict(task_d)
            tv.status = overall_status
            await self._data.put(task_key, tv.to_dict())

        return ParsingCommandResult(uid=uid, status=overall_status)

    async def _parse_model(
        self, uid: int, model_name: str, mode: str,
    ) -> int:
        """Parse a single model for a uid. Returns the number of items parsed."""
        return await self._materializer.parse_model(uid, model_name, mode)

    async def _download_images(self, uid: int) -> dict[str, Any]:
        """Download images for all parsed models. Returns summary dict."""
        return await self._materializer.download_images(uid)

    async def _model_has_existing_items(self, uid: int, model_name: str) -> bool:
        """Return whether a model has any parsed objects already stored."""
        return bool(await self._load_typed_objects(uid, model_name))

    async def _load_typed_objects(
        self, uid: int, model_name: str,
    ) -> list[dict[str, Any]]:
        """Load all typed objects for a model from the data store."""
        prefix = f"uid:{uid}:parse:{model_name}:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def delete_uid(self, uid: int) -> dict[str, int]:
        """Delete all parsing state for a uid. Returns counts."""
        data_count = await self._data.delete_by_uid_prefix(uid)
        # Remove downloaded images directory
        images_dir = self._data.base / str(uid) / "images"
        images_existed = images_dir.exists()
        if images_existed:
            shutil.rmtree(images_dir, ignore_errors=True)
        return {"data": data_count, "images_dir_removed": int(images_existed)}

    async def close(self) -> None:
        """Close underlying stores."""
        await self._data.close()
