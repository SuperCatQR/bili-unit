# transform/articles — 专栏文章 transform handler.
#
# Consumes an Article typed-object dict from the parsing store.
# The parsing model has already merged image_urls, extracted stats,
# readlist membership, and article_detail markdown/content_json.
# Transform projects the typed-object dict into the canonical
# structured result.

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "articles"
SOURCE_ENDPOINTS: tuple[str, ...] = (
    "articles",
    "article_detail",
    "article_list_detail",
)
OPTIONAL_ENDPOINTS: tuple[str, ...] = (
    "article_detail",
    "article_list_detail",
)


class _ArticlesHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    optional_endpoints = OPTIONAL_ENDPOINTS

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Project an Article typed-object dict into a structured result."""
        d = item.item_data

        markdown_text: str = d.get("markdown", "")
        content_json: list[Any] = d.get("content_json", [])

        # readlist (文集) membership — serialised as [{rlid, name}, ...]
        lists_in = d.get("lists", [])
        lists_out: list[dict[str, str]] = []
        for m in lists_in:
            if isinstance(m, dict):
                lists_out.append({
                    "rlid": str(m.get("rlid", "")),
                    "name": str(m.get("name", "")),
                })

        return {
            "id": item.item_id,
            "title": d.get("title", ""),
            "summary": d.get("summary", ""),
            "image_urls": list(d.get("image_urls", [])),
            "stats": dict(d.get("stats", {})),
            "ctime": d.get("ctime"),
            "lists": lists_out,
            "markdown": markdown_text,
            "content_json": content_json,
            "word_count": len(markdown_text),
        }


HANDLER: TransformHandler = _ArticlesHandler()
