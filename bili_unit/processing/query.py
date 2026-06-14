# query — processing read-only view.
#
# ProcessingQuery exposes the read-only DTO surface. get_video_full /
# list_all_videos read metadata from parsing.query (VideoDetail typed
# objects) and transcription from the audio pipeline items in the
# processing store.

from __future__ import annotations

import logging
import time
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
    from ..parsing.query import ParsingQuery
    from .data import ProcessingDataStore
    from .error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.query")


class ProcessingQuery:
    """Read-only access to processing results / task state / errors."""

    def __init__(
        self,
        data: ProcessingDataStore,
        error: ProcessingErrorStore,
        fetching_query: FetchingQuery | None = None,
        parsing_query: ParsingQuery | None = None,
    ) -> None:
        self._data = data
        self._error = error
        self._fetch_qry = fetching_query
        self._parse_qry = parsing_query

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
        """Return a VideoFullDTO combining parsing metadata + audio transcription.

        metadata is a virtual ProcessingItemDTO constructed from the parsing
        VideoDetail dict (pipeline="parsing", status=SUCCESS).  If parsing
        has no record for this bvid the video is considered absent.
        """
        if self._parse_qry is not None:
            parsing_dict = await self._parse_qry.get_video_detail(uid, bvid)
        else:
            parsing_dict = None

        if parsing_dict is None:
            return None

        metadata = self._parsing_dict_to_item_dto(uid, bvid, parsing_dict)
        transcription = await self.get_item(uid, "audio", bvid)
        return VideoFullDTO(bvid=bvid, metadata=metadata, transcription=transcription)

    async def list_all_videos(self, uid: int) -> list[VideoSummaryDTO]:
        """List all videos with metadata from parsing + transcription status from audio."""
        if self._parse_qry is not None:
            parsing_dicts = await self._parse_qry.list_video_details(uid)
        else:
            parsing_dicts = []

        out: list[VideoSummaryDTO] = []
        for d in parsing_dicts:
            bvid = d.get("bvid", "")
            if not bvid:
                continue
            transcription_dto = await self.get_item(uid, "audio", bvid)
            out.append(VideoSummaryDTO(
                bvid=bvid,
                title=d.get("title", ""),
                status=ProcessingItemStatus.SUCCESS,
                has_transcription=transcription_dto is not None
                    and transcription_dto.status == ProcessingItemStatus.SUCCESS,
                duration=d.get("duration"),
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

    @staticmethod
    def _parsing_dict_to_item_dto(uid: int, bvid: str, d: dict) -> ProcessingItemDTO:
        """Construct a virtual ProcessingItemDTO from a parsing VideoDetail dict.

        The caller (e.g. get_video_full) needs a ProcessingItemDTO for metadata
        to keep the VideoFullDTO contract stable.  We synthesise one with
        pipeline="parsing" and status=SUCCESS so existing callers can access
        result fields (title, duration, tags) without code changes.
        """
        processed_at = d.get("_updated_at") or int(time.time() * 1000)
        return ProcessingItemDTO(
            uid=uid,
            pipeline="parsing",
            item_type="video_detail",
            item_id=bvid,
            status=ProcessingItemStatus.SUCCESS,
            result=d,
            processed_at=processed_at,
            errors=[],
        )
