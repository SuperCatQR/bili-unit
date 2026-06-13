from __future__ import annotations

from typing import Any

from bili_unit.parsing.models.content_post import (
    ContentPost,
    CrossRefs,
    SourceRef,
    content_key_for_refs,
)

from ._common import (
    dedup_source_refs,
    dedup_strings,
    detail_text_from_content_json,
    dict_or_empty,
    int_or_none,
    pages_items,
    stats_dict,
    str_or_empty,
)


def _article_details_by_cvid(payloads: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payloads, dict):
        return {}

    details: dict[str, dict[str, Any]] = {}
    for key, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        cvid = str_or_empty(key)
        info = dict_or_empty(payload.get("info"))
        if not cvid:
            cvid = str_or_empty(info.get("id") or info.get("cvid"))
        if cvid:
            details[cvid] = payload
    return details


def _article_list_refs_by_cvid(payloads: Any) -> dict[str, list[SourceRef]]:
    if not isinstance(payloads, dict):
        return {}

    refs_by_cvid: dict[str, list[SourceRef]] = {}
    for rlid, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        ref = SourceRef("article_list_detail", str_or_empty(rlid))
        for article in pages_items(payload, "articles"):
            cvid = str_or_empty(article.get("id") or article.get("cvid"))
            if cvid:
                refs_by_cvid.setdefault(cvid, []).append(ref)
    return refs_by_cvid


def _article_images(list_item: dict[str, Any], detail_info: dict[str, Any]) -> list[str]:
    return dedup_strings(
        detail_info.get("image_urls"),
        detail_info.get("origin_image_urls"),
        detail_info.get("banner_url"),
        list_item.get("image_urls"),
        list_item.get("origin_image_urls"),
        list_item.get("banner_url"),
    )


def _article_pub_time(list_item: dict[str, Any], detail_info: dict[str, Any]) -> int | None:
    return (
        int_or_none(detail_info.get("ctime"))
        or int_or_none(detail_info.get("publish_time"))
        or int_or_none(detail_info.get("pub_time"))
        or int_or_none(list_item.get("ctime"))
        or int_or_none(list_item.get("publish_time"))
        or int_or_none(list_item.get("pub_time"))
    )


def select_article_posts(
    raw_articles_payload: dict[str, Any] | None,
    article_detail_payloads: dict[str, dict[str, Any]] | None = None,
    article_list_detail_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[ContentPost]:
    details = _article_details_by_cvid(article_detail_payloads)
    list_refs_by_cvid = _article_list_refs_by_cvid(article_list_detail_payloads)
    posts: list[ContentPost] = []

    for list_item in pages_items(raw_articles_payload, "articles"):
        cvid = str_or_empty(list_item.get("id") or list_item.get("cvid"))
        if not cvid:
            continue

        detail = details.get(cvid, {})
        detail_info = dict_or_empty(detail.get("info"))
        cross_refs = CrossRefs(cvid=cvid)
        markdown = str_or_empty(detail.get("markdown"))
        text = detail_text_from_content_json(detail.get("content_json"))

        source_refs = dedup_source_refs([
            SourceRef("articles", cvid),
            SourceRef("article_detail", cvid) if detail else SourceRef("", ""),
            *list_refs_by_cvid.get(cvid, []),
        ])

        posts.append(
            ContentPost(
                content_key=content_key_for_refs(cross_refs),
                kind="article",
                title=str_or_empty(detail_info.get("title") or list_item.get("title")),
                summary=str_or_empty(detail_info.get("summary") or list_item.get("summary")),
                text=text,
                markdown=markdown,
                images=_article_images(list_item, detail_info),
                pub_time=_article_pub_time(list_item, detail_info),
                stats=stats_dict(detail_info.get("stats") or list_item.get("stats")),
                source_refs=source_refs,
                cross_refs=cross_refs,
            ),
        )

    return posts
