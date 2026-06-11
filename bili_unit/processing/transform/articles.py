# transform/articles — 专栏文章 transform handler.
#
# Per docs/design/processing.md §6.4:
#   输入: articles 的 raw_payload，结构 {pages: [{articles: [...], ...}]}
#   输出: 每篇 article 一个结构化 dict（仅列表级字段；全文留作后续扩展）

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "articles"
SOURCE_ENDPOINTS: tuple[str, ...] = ("articles",)


class _ArticlesHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        rp = raw_payloads.get("articles") or {}
        items: list[WorkItem] = []
        for page in rp.get("pages", []) or []:
            if not isinstance(page, dict):
                continue
            for art in page.get("articles", []) or []:
                if not isinstance(art, dict):
                    continue
                aid = art.get("id")
                if aid is None:
                    continue
                # article_id 是 int；统一为 string 作为 store key 占位符
                items.append(WorkItem(
                    item_type=ITEM_TYPE,
                    item_id=str(aid),
                    item_data=art,
                ))
        return items

    def transform(self, item: WorkItem) -> dict[str, Any]:
        art = item.item_data

        # image_urls：B 站列表项 image_urls / banner_url 不一致，做容错合并
        image_urls: list[str] = []
        for key in ("image_urls", "origin_image_urls"):
            arr = art.get(key)
            if isinstance(arr, list):
                for u in arr:
                    if isinstance(u, str) and u:
                        image_urls.append(u)
        banner = art.get("banner_url")
        if isinstance(banner, str) and banner:
            image_urls.append(banner)

        stats_in = art.get("stats")
        if not isinstance(stats_in, dict):
            stats_in = {}
        stats_out = {
            "view": stats_in.get("view", 0),
            "favorite": stats_in.get("favorite", 0),
            "like": stats_in.get("like", 0),
            "reply": stats_in.get("reply", 0),
            "share": stats_in.get("share", 0),
            "coin": stats_in.get("coin", 0),
        }

        return {
            "id": item.item_id,
            "title": art.get("title") or "",
            "summary": art.get("summary") or "",
            "image_urls": image_urls,
            "stats": stats_out,
            "ctime": art.get("publish_time") or art.get("ctime"),
        }


HANDLER: TransformHandler = _ArticlesHandler()
