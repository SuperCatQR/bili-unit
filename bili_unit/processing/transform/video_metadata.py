# transform/video_metadata — 视频元数据 transform handler.
#
# Consumes a VideoDetail typed-object dict from the parsing store.
# The parsing model has already extracted and validated all fields;
# transform is a thin projection from typed-object → structured result.

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "video_metadata"
SOURCE_ENDPOINTS: tuple[str, ...] = ("video_detail",)


class _VideoMetadataHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Project a VideoDetail typed-object dict into a structured result."""
        d = item.item_data

        # pages: keep cid, part, duration, dimension (drop first_frame)
        pages_out: list[dict[str, Any]] = []
        for p in d.get("pages", []):
            pages_out.append({
                "cid": p.get("cid"),
                "part": p.get("part", ""),
                "duration": p.get("duration", 0),
                "dimension": p.get("dimension", {}),
            })

        return {
            "bvid": d.get("bvid", ""),
            "aid": d.get("aid"),
            "title": d.get("title", ""),
            "desc": d.get("desc", ""),
            "duration": d.get("duration", 0),
            "pages": pages_out,
            "tags": list(d.get("tags", [])),
            "stat": dict(d.get("stat", {})),
            "owner": dict(d.get("owner", {})),
            "ctime": d.get("ctime"),
            "pubdate": d.get("pubdate"),
            "rights": dict(d.get("rights", {})),
            "subtitle": dict(d.get("subtitle", {})),
            "label": dict(d.get("label", {})),
        }


HANDLER: TransformHandler = _VideoMetadataHandler()
