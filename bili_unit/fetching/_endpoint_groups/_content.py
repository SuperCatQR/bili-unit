# Auto-split from _endpoint_catalog; endpoint facts stay unchanged.

from __future__ import annotations

from .._bilibili_adapter import (
    _extract_cvids_from_articles,
    _extract_opus_ids_from_opus,
    _extract_rlids_from_article_list,
    fetch_article_detail_item,
    fetch_article_list_detail_item,
    fetch_opus_detail_item,
)
from .._endpoint_spec import EndpointSpec


def _skip_legacy_article_detail_item(item: dict) -> str | None:
    try:
        template_id = item.get("template_id")
        origin_template_id = item.get("origin_template_id")
        category = item.get("category") or {}
        category_id = category.get("id")
        type_id = item.get("type")
    except AttributeError:
        return None

    if (
        template_id == 4
        and origin_template_id == 5
        and category_id in {41, 42}
        and type_id in {2, 3, 4, 0}
    ):
        return "legacy article body endpoint skips note/opus-style content"
    return None


def content_endpoints() -> list[EndpointSpec]:
    return [
        # --- item-level fan-out: article_detail (专栏正文) ---
        EndpointSpec(
            name="article_detail",
            callable=fetch_article_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="article_detail",
            kind="item",
            source_endpoint="articles",
            extract_items=_extract_cvids_from_articles,
            skip_item=_skip_legacy_article_detail_item,
        ),
        # --- item-level fan-out: opus_detail (图文正文 + 图片清单) ---
        EndpointSpec(
            name="opus_detail",
            callable=fetch_opus_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="opus_detail",
            kind="item",
            source_endpoint="opus",
            extract_items=_extract_opus_ids_from_opus,
        ),
        # --- item-level fan-out: article_list_detail (文集 → 文章 cvid 清单) ---
        EndpointSpec(
            name="article_list_detail",
            callable=fetch_article_list_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="article_list_detail",
            kind="item",
            source_endpoint="article_list",
            extract_items=_extract_rlids_from_article_list,
        ),
    ]

__all__ = ["content_endpoints"]
