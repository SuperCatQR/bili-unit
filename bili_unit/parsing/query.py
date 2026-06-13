# query — parsing read-only view; returns DTOs, never exposes store internals.

from __future__ import annotations

import logging
from typing import Any

from . import (
    ParsingImageDTO,
    ParsingModelDTO,
    ParsingModelStatus,
    ParsingTaskDTO,
    ParsingTaskStatus,
    ParsingTaskValue,
)
from .data import ParsingDataStore
from .keys import _item_key, _task_key

logger = logging.getLogger("bili.parsing.query")

# All 5 model names
MODEL_NAMES: tuple[str, ...] = (
    "user_profile",
    "video_detail",
    "article",
    "opus",
    "dynamic",
)


class ParsingQuery:
    """Read-only interface to parsing results."""

    def __init__(self, data: ParsingDataStore) -> None:
        self._data = data

    # -- task --------------------------------------------------------------

    async def get_task(self, uid: int) -> ParsingTaskDTO | None:
        """Return the parsing task DTO for a uid, or None."""
        d = await self._data.get(_task_key(uid))
        if d is None:
            return None
        tv = ParsingTaskValue.from_dict(d)
        model_dtos: dict[str, ParsingModelDTO] = {}
        for model, entry in tv.models.items():
            try:
                status = ParsingModelStatus(entry.get("status", "PENDING"))
            except ValueError:
                status = ParsingModelStatus.PENDING
            model_dtos[model] = ParsingModelDTO(
                model=model,
                status=status,
                count=entry.get("count", 0),
            )
        images = None
        if tv.images is not None:
            images = ParsingImageDTO(
                total=tv.images.get("total", 0),
                ok=tv.images.get("ok", 0),
                skipped=tv.images.get("skipped", 0),
                failed=tv.images.get("failed", 0),
                failed_urls=tv.images.get("failed_urls", []),
            )
        return ParsingTaskDTO(
            uid=tv.uid,
            status=tv.status,
            models=model_dtos,
            images=images,
            created_at=tv.created_at,
            updated_at=tv.updated_at,
        )

    async def list_tasks(self) -> list[dict[str, Any]]:
        """Return a lightweight summary of all parsing tasks."""
        all_rows = await self._data.list_prefix("uid:")
        results: list[dict[str, Any]] = []
        for key, value in all_rows:
            if not key.endswith(":task"):
                continue
            parts = key.split(":")
            if len(parts) != 3:
                continue
            try:
                uid = int(parts[1])
            except ValueError:
                continue
            try:
                status = ParsingTaskStatus(value.get("status", "PENDING"))
            except ValueError:
                status = ParsingTaskStatus.PENDING
            model_count = len(value.get("models", {}))
            results.append({
                "uid": uid,
                "status": status,
                "model_count": model_count,
                "updated_at": value.get("updated_at"),
            })
        results.sort(key=lambda r: r["uid"])
        return results

    # -- typed object accessors --------------------------------------------

    async def get_user_profile(self, uid: int) -> dict[str, Any] | None:
        """Return the UpProfile typed object as a dict, or None."""
        return await self._data.get(_item_key(uid, "user_profile", str(uid)))

    async def list_video_details(self, uid: int) -> list[dict[str, Any]]:
        """Return all VideoDetail typed objects for a uid."""
        prefix = f"uid:{uid}:parse:video_detail:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def get_video_detail(self, uid: int, bvid: str) -> dict[str, Any] | None:
        return await self._data.get(_item_key(uid, "video_detail", bvid))

    async def list_articles(self, uid: int) -> list[dict[str, Any]]:
        prefix = f"uid:{uid}:parse:article:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def get_article(self, uid: int, cvid: str) -> dict[str, Any] | None:
        return await self._data.get(_item_key(uid, "article", cvid))

    async def list_opus(self, uid: int) -> list[dict[str, Any]]:
        prefix = f"uid:{uid}:parse:opus:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def get_opus(self, uid: int, opus_id: str) -> dict[str, Any] | None:
        return await self._data.get(_item_key(uid, "opus", opus_id))

    async def list_dynamics(self, uid: int) -> list[dict[str, Any]]:
        prefix = f"uid:{uid}:parse:dynamic:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def get_dynamic(self, uid: int, dynamic_id: str) -> dict[str, Any] | None:
        return await self._data.get(_item_key(uid, "dynamic", dynamic_id))
