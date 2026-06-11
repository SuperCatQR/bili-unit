# transform/video_metadata — 视频元数据 transform handler.
#
# Per docs/design/processing.md §6.2:
#   输入: video_detail/{bvid} 的 raw_payload，结构 {info: {...}, tags: [...]}
#   输出: 结构化 video_metadata 记录（见 §5.3 value 形状）
#
# raw_payload 来源：fetching/video_detail item-level fan-out 的 per-bvid 结果。
# 单个工作项 = 单个 bvid 的处理。

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "video_metadata"
SOURCE_ENDPOINTS: tuple[str, ...] = ("video_detail",)


class _VideoMetadataHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        """Return one WorkItem per bvid present in raw_payloads.

        ``raw_payloads`` maps endpoint name → that endpoint's raw_payload
        (per-bvid for video_detail; per-uid pages for uid-level endpoints).
        For video_metadata MVP we only need video_detail. The runner is
        responsible for feeding *one* WorkItem per bvid by reading
        ``fetching.query.list_video_details`` upstream — the runner then
        calls ``extract_items({"video_detail": single_bvid_payload})``
        for each item, so this handler simply normalises the single payload.
        """
        vd = raw_payloads.get("video_detail")
        if not isinstance(vd, dict):
            return []
        info = vd.get("info") or {}
        bvid = info.get("bvid")
        if not bvid or not isinstance(bvid, str):
            return []
        return [WorkItem(item_type=ITEM_TYPE, item_id=bvid, item_data=vd)]

    def transform(self, item: WorkItem) -> dict[str, Any]:
        info: dict[str, Any] = item.item_data.get("info") or {}
        tags_raw = item.item_data.get("tags") or []

        bvid = info.get("bvid") or item.item_id
        aid = info.get("aid")
        title = info.get("title") or ""
        desc = info.get("desc") or ""
        duration = info.get("duration") or 0
        ctime = info.get("ctime")
        pubdate = info.get("pubdate")

        # pages: extract a stable subset (cid, part, duration, dimension)
        pages_in: list[dict] = info.get("pages") or []
        pages_out: list[dict[str, Any]] = []
        for p in pages_in:
            if not isinstance(p, dict):
                continue
            pages_out.append({
                "cid": p.get("cid"),
                "part": p.get("part") or "",
                "duration": p.get("duration") or 0,
                "dimension": p.get("dimension") or {},
            })

        # tags: list of tag_name strings (lossless source kept in raw via fetching)
        tags_out: list[str] = []
        for t in tags_raw:
            if isinstance(t, dict):
                name = t.get("tag_name")
                if isinstance(name, str) and name:
                    tags_out.append(name)
            elif isinstance(t, str):
                tags_out.append(t)

        # stat: copy a known subset (missing fields → 0)
        stat_in: dict[str, Any] = info.get("stat") or {}
        stat_keys = ("view", "danmaku", "reply", "favorite", "coin", "share", "like")
        stat_out = {k: stat_in.get(k, 0) for k in stat_keys}

        # owner: trimmed to (mid, name, face)
        owner_in: dict[str, Any] = info.get("owner") or {}
        owner_out = {
            "mid": owner_in.get("mid"),
            "name": owner_in.get("name") or "",
            "face": owner_in.get("face") or "",
        }

        return {
            "bvid": bvid,
            "aid": aid,
            "title": title,
            "desc": desc,
            "duration": duration,
            "pages": pages_out,
            "tags": tags_out,
            "stat": stat_out,
            "owner": owner_out,
            "ctime": ctime,
            "pubdate": pubdate,
            "rights": info.get("rights") or {},
            "subtitle": info.get("subtitle") or {},
            "label": info.get("label") or {},
        }


HANDLER: TransformHandler = _VideoMetadataHandler()
