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
