# bili_unit.query — unified read-side entry across stages.
#
# Per docs/structure/bili.md §10, the bili unit's outward read entry is the
# `query/` package. Its job is to expose read-only views over each stage's
# data / error stores.
#
# Boundaries (docs/structure/bili.md §8):
#   - query 不暴露 data / error 内部存储结构
#   - query 不做跨源归一化语义处理
#   - query 不触发写侧流程
#   - query 不读取 raw / temp

from __future__ import annotations

from typing import TYPE_CHECKING

from .._aggregates import VideoFullDTO, VideoSummaryDTO
from ..fetching.query import Query as _FetchingQuery

if TYPE_CHECKING:
    from ..parsing.query import ParsingQuery as _ParsingQuery
    from ..processing.query import ProcessingQuery as _ProcessingQuery


class BiliQuery:
    """Bili unit的统一只读入口；持有各阶段 query 实例。"""

    def __init__(
        self,
        fetching: _FetchingQuery,
        parsing: _ParsingQuery | None = None,
        processing: _ProcessingQuery | None = None,
    ) -> None:
        self._fetching = fetching
        self._parsing = parsing
        self._processing = processing

    @property
    def fetching(self) -> _FetchingQuery:
        return self._fetching

    @property
    def parsing(self) -> _ParsingQuery:
        if self._parsing is None:
            raise RuntimeError("parsing query was not assembled")
        return self._parsing

    @property
    def processing(self) -> _ProcessingQuery:
        if self._processing is None:
            raise RuntimeError("processing query was not assembled")
        return self._processing

    # -- cross-stage aggregate views --------------------------------------

    async def get_video_full(self, uid: int, bvid: str) -> VideoFullDTO | None:
        """Combine parsing metadata + audio transcription for one bvid.

        Returns None when parsing has no record for this bvid (the source
        of truth for "does this video exist for this uid").
        """
        if self._parsing is None:
            raise RuntimeError("parsing query was not assembled")
        if self._processing is None:
            raise RuntimeError("processing query was not assembled")
        metadata = await self._parsing.get_video_detail(uid, bvid)
        if metadata is None:
            return None
        transcription = await self._processing.get_item(uid, "audio", bvid)
        return VideoFullDTO(
            bvid=bvid, metadata=metadata, transcription=transcription,
        )

    async def list_all_videos(self, uid: int) -> list[VideoSummaryDTO]:
        """List all videos with metadata from parsing + transcription status from audio."""
        if self._parsing is None:
            raise RuntimeError("parsing query was not assembled")
        if self._processing is None:
            raise RuntimeError("processing query was not assembled")
        from ..processing import ProcessingItemStatus  # local to avoid cycle

        metadata_dicts = await self._parsing.list_video_details(uid)
        out: list[VideoSummaryDTO] = []
        for d in metadata_dicts:
            bvid = d.get("bvid", "")
            if not bvid:
                continue
            item = await self._processing.get_item(uid, "audio", bvid)
            out.append(VideoSummaryDTO(
                bvid=bvid,
                title=d.get("title", ""),
                has_transcription=(
                    item is not None
                    and item.status == ProcessingItemStatus.SUCCESS
                ),
                duration=d.get("duration"),
            ))
        return out


__all__ = ["BiliQuery"]
