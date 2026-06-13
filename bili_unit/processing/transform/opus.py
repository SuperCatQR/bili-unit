# transform/opus — 图文 transform handler.
#
# Consumes an OpusPost typed-object dict from the parsing store.
# The parsing model has already extracted title, summary, cover,
# jump_url, stats, list_images, detail markdown/images.
# Transform projects the typed-object dict into the canonical
# structured result, merging cover + body images into a unified
# image_urls list.

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "opus"
SOURCE_ENDPOINTS: tuple[str, ...] = ("opus", "opus_detail")
OPTIONAL_ENDPOINTS: tuple[str, ...] = ("opus_detail",)


class _OpusHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    optional_endpoints = OPTIONAL_ENDPOINTS

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Project an OpusPost typed-object dict into a structured result."""
        d = item.item_data

        cover_url: str = d.get("cover", "")
        detail_images_raw = d.get("detail_images", [])
        detail_images: list[dict[str, Any]] = [
            img for img in detail_images_raw if isinstance(img, dict)
        ]

        # Build unified image_urls: prefer detail images, fallback to list_images,
        # then cover as last resort.
        if detail_images:
            image_urls: list[str] = []
            for img in detail_images:
                u = img.get("url", "")
                if isinstance(u, str) and u:
                    image_urls.append(u)
        elif d.get("list_images"):
            image_urls = list(d["list_images"])
        elif cover_url:
            image_urls = [cover_url]
        else:
            image_urls = []

        markdown_text: str = d.get("markdown", "")

        return {
            "id": item.item_id,
            "title": d.get("title", ""),
            "summary": d.get("summary", ""),
            "image_urls": image_urls,
            "stats": dict(d.get("stats", {})),
            "ctime": d.get("ctime"),
            "jump_url": d.get("jump_url", ""),
            "markdown": markdown_text,
            "images": detail_images,
            "word_count": len(markdown_text),
        }


HANDLER: TransformHandler = _OpusHandler()
