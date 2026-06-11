# query — processing read-only view.
#
# Per docs/design/processing.md §11.2: ProcessingQuery exposes the
# read-only DTO surface. It MAY read fetching.query for joined views
# (e.g. video_full) but never exposes fetching's internal store layout.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import (
    ErrorDTO,
    ProcessingItemDTO,
    ProcessingItemStatus,
    ProcessingPipelineDTO,
    ProcessingTaskDTO,
    ProcessingTaskStatus,
    VideoFullDTO,
    VideoSummaryDTO,
)
from .keys import _proc_key, _task_key
from .task import ProcessingTaskValue

if TYPE_CHECKING:
    from ..fetching.query import Query as FetchingQuery
    from .data import ProcessingDataStore
    from .error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.query")


class ProcessingQuery:
    """Read-only access to processing results / task state / errors."""

    def __init__(
        self,
        data: ProcessingDataStore,
        error: ProcessingErrorStore,
        fetching_query: FetchingQuery,
    ) -> None:
        self._data = data
        self._error = error
        self._fetch_qry = fetching_query

    # -- task / pipeline / item -------------------------------------------

    async def get_task(self, uid: int) -> ProcessingTaskDTO | None:
        d = await self._data.get(_task_key(uid))
        if d is None:
            return None
        tv = ProcessingTaskValue.from_dict(d)
        pipelines: dict[str, ProcessingPipelineDTO] = {}
        for name, entry in tv.pipelines.items():
            pipelines[name] = ProcessingPipelineDTO(
                name=name,
                status=entry.status,
                items=dict(entry.items),
            )
        return ProcessingTaskDTO(
            uid=tv.uid,
            status=tv.status,
            pipelines=pipelines,
            created_at=tv.created_at,
            updated_at=tv.updated_at,
        )

    async def list_tasks(self) -> list[dict]:
        all_rows = await self._data.list_prefix("uid:")
        results: list[dict] = []
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
                status = ProcessingTaskStatus(value.get("status", "PENDING"))
            except ValueError:
                status = ProcessingTaskStatus.PENDING
            pipeline_count = len(value.get("pipelines", {}))
            results.append({
                "uid": uid,
                "status": status,
                "pipeline_count": pipeline_count,
                "updated_at": value.get("updated_at"),
            })
        results.sort(key=lambda r: r["uid"])
        return results

    async def get_item(
        self, uid: int, item_type: str, item_id: str,
    ) -> ProcessingItemDTO | None:
        d = await self._data.get(_proc_key(uid, item_type, item_id))
        if d is None:
            return None
        return self._to_item_dto(d)

    async def list_items(self, uid: int, item_type: str) -> list[ProcessingItemDTO]:
        prefix = f"uid:{uid}:proc:{item_type}:"
        rows = await self._data.list_prefix(prefix)
        return [self._to_item_dto(v) for _, v in rows]

    # -- aggregate views ---------------------------------------------------

    async def get_video_full(self, uid: int, bvid: str) -> VideoFullDTO | None:
        meta = await self.get_item(uid, "video_metadata", bvid)
        if meta is None:
            # If processing has nothing for this bvid, fall back to fetching
            # to show whether the raw upstream exists.
            fetched = await self._fetch_qry.get_video_detail(uid, bvid)
            if fetched is None:
                return None
            return VideoFullDTO(bvid=bvid, metadata=None, transcription=None)
        transcription = await self.get_item(uid, "audio", bvid)
        return VideoFullDTO(bvid=bvid, metadata=meta, transcription=transcription)

    async def list_all_videos(self, uid: int) -> list[VideoSummaryDTO]:
        items = await self.list_items(uid, "video_metadata")
        out: list[VideoSummaryDTO] = []
        for dto in items:
            result = dto.result or {}
            transcription_dto = await self.get_item(uid, "audio", dto.item_id)
            out.append(VideoSummaryDTO(
                bvid=dto.item_id,
                title=result.get("title", ""),
                status=dto.status,
                has_transcription=transcription_dto is not None
                    and transcription_dto.status == ProcessingItemStatus.SUCCESS,
                duration=result.get("duration"),
            ))
        return out

    # -- errors -----------------------------------------------------------

    async def list_errors(self, uid: int | None = None) -> list[ErrorDTO]:
        return await self._error.list_errors(uid=uid)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _to_item_dto(d: dict) -> ProcessingItemDTO:
        try:
            status = ProcessingItemStatus(d.get("status", "PENDING"))
        except ValueError:
            status = ProcessingItemStatus.PENDING
        return ProcessingItemDTO(
            uid=d["uid"],
            pipeline=d["pipeline"],
            item_type=d["item_type"],
            item_id=d["item_id"],
            status=status,
            result=d.get("result"),
            processed_at=d.get("processed_at"),
            errors=[],
        )
