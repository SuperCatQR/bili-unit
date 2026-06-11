# transform/articles — 专栏文章 transform handler.
#
# Per docs/feature/processing.md:
#   输入: articles 的 raw_payload，结构 {pages: [{articles: [...], ...}]}
#         + article_detail 的 per-cvid raw_payload，结构 {info, markdown, content_json}
#   输出: 每篇 article 一个结构化 dict（列表级字段 + 正文 markdown / content_json）
#
# article_detail is treated as an *optional* enrichment endpoint:
#   - 若某 cvid 拉到了 article_detail.SUCCESS，正文 markdown / content_json /
#     word_count 一并写入 result。
#   - 拉不到（删除、私密、fetching 还没跑过 article_detail）则降级回列表级字段，
#     与 article_detail 接入前的旧行为兼容。

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "articles"
SOURCE_ENDPOINTS: tuple[str, ...] = ("articles", "article_detail")
OPTIONAL_ENDPOINTS: tuple[str, ...] = ("article_detail",)


class _ArticlesHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    optional_endpoints = OPTIONAL_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        """Build one WorkItem per article from the listing payload.

        ``article_detail`` is attached at transform-time, not item-time:
        the runner pre-collected ``{cvid → detail_payload}`` and stashes it
        under ``raw_payloads["article_detail"]``.  ``transform`` looks it up
        per-item; missing entries fall back to list-level fields.
        """
        rp = raw_payloads.get("articles") or {}
        details = raw_payloads.get("article_detail") or {}
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
                cvid_str = str(aid)
                # article_id 是 int；统一为 string 作为 store key 占位符
                items.append(WorkItem(
                    item_type=ITEM_TYPE,
                    item_id=cvid_str,
                    item_data={
                        "list": art,
                        # Detail payload may be None when fetching layer
                        # has not yet pulled article_detail for this cvid;
                        # transform() handles that gracefully.
                        "detail": details.get(cvid_str),
                    },
                ))
        return items

    def transform(self, item: WorkItem) -> dict[str, Any]:
        data = item.item_data
        # extract_items always wraps as {"list": ..., "detail": ...}, but
        # tolerate the legacy bare-dict shape for callers that hand-craft
        # WorkItems in tests.
        if isinstance(data, dict) and "list" in data and "detail" in data:
            art = data.get("list") or {}
            detail = data.get("detail")
        else:
            art = data
            detail = None

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

        # Detail-level enrichment: markdown body + content node tree + word
        # count (computed from markdown stripped of newlines, character-count
        # since Chinese articles dominate the corpus).
        markdown_text: str = ""
        content_json: list[Any] = []
        if isinstance(detail, dict):
            md = detail.get("markdown")
            if isinstance(md, str):
                markdown_text = md
            cj = detail.get("content_json")
            if isinstance(cj, list):
                content_json = cj

        return {
            "id": item.item_id,
            "title": art.get("title") or "",
            "summary": art.get("summary") or "",
            "image_urls": image_urls,
            "stats": stats_out,
            "ctime": art.get("publish_time") or art.get("ctime"),
            "markdown": markdown_text,
            "content_json": content_json,
            "word_count": len(markdown_text),
        }


HANDLER: TransformHandler = _ArticlesHandler()
