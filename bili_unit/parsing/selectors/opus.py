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
    dict_or_empty,
    int_or_none,
    module_map,
    pages_items,
    stats_dict,
    str_or_empty,
)


def _opus_details_by_id(payloads: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payloads, dict):
        return {}

    details: dict[str, dict[str, Any]] = {}
    for key, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        opus_id = str_or_empty(key)
        info = dict_or_empty(payload.get("info"))
        item = dict_or_empty(info.get("item"))
        basic = dict_or_empty(item.get("basic"))
        if not opus_id:
            opus_id = str_or_empty(item.get("id") or item.get("opus_id") or basic.get("opus_id"))
        if opus_id:
            details[opus_id] = payload
    return details


def _major_opus(raw: dict[str, Any]) -> dict[str, Any]:
    modules = module_map(raw.get("modules"))
    module_dynamic = dict_or_empty(modules.get("module_dynamic"))
    major = dict_or_empty(module_dynamic.get("major"))
    return dict_or_empty(major.get("opus"))


def _detail_item(detail: dict[str, Any]) -> dict[str, Any]:
    info = dict_or_empty(detail.get("info"))
    return dict_or_empty(info.get("item"))


def _summary_from_opus_block(opus: dict[str, Any]) -> str:
    summary = dict_or_empty(opus.get("summary"))
    return str_or_empty(summary.get("text"))


def _images_from_opus_block(opus: dict[str, Any]) -> list[str]:
    pics = opus.get("pics")
    if not isinstance(pics, list):
        return []
    return [
        url
        for pic in pics
        if isinstance(pic, dict)
        for url in [str_or_empty(pic.get("url") or pic.get("src"))]
        if url
    ]


def _images_from_detail(detail: dict[str, Any]) -> list[str]:
    images = detail.get("images")
    if not isinstance(images, list):
        return []
    return [
        url
        for image in images
        if isinstance(image, dict)
        for url in [str_or_empty(image.get("url") or image.get("src"))]
        if url
    ]


def _opus_pub_time(list_item: dict[str, Any], detail_item: dict[str, Any]) -> int | None:
    return (
        int_or_none(detail_item.get("pub_time"))
        or int_or_none(detail_item.get("ctime"))
        or int_or_none(list_item.get("pub_time"))
        or int_or_none(list_item.get("ctime"))
    )


def select_opus_posts(
    raw_opus_payload: dict[str, Any] | None,
    opus_detail_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[ContentPost]:
    details = _opus_details_by_id(opus_detail_payloads)
    posts: list[ContentPost] = []

    for list_item in pages_items(raw_opus_payload, "items"):
        opus_id = str_or_empty(list_item.get("opus_id") or list_item.get("id"))
        if not opus_id:
            continue

        detail = details.get(opus_id, {})
        detail_item = _detail_item(detail)
        opus_block = _major_opus(list_item)
        detail_opus_block = _major_opus(detail_item)
        cross_refs = CrossRefs(opus_id=opus_id)
        detail_images = _images_from_detail(detail)
        list_images = _images_from_opus_block(opus_block)
        detail_block_images = _images_from_opus_block(detail_opus_block)

        source_refs = dedup_source_refs([
            SourceRef("opus", opus_id),
            SourceRef("opus_detail", opus_id) if detail else SourceRef("", ""),
        ])

        posts.append(
            ContentPost(
                content_key=content_key_for_refs(cross_refs),
                kind="opus",
                title=str_or_empty(detail_item.get("title") or list_item.get("title")),
                summary=str_or_empty(
                    detail_item.get("summary")
                    or list_item.get("summary")
                    or _summary_from_opus_block(detail_opus_block)
                    or _summary_from_opus_block(opus_block),
                ),
                text=str_or_empty(detail.get("markdown")) or _summary_from_opus_block(detail_opus_block),
                markdown=str_or_empty(detail.get("markdown")),
                images=dedup_strings(
                    detail_images,
                    detail_block_images,
                    list_images,
                    detail_item.get("cover"),
                    list_item.get("cover"),
                ),
                pub_time=_opus_pub_time(list_item, detail_item),
                stats=stats_dict(detail_item.get("stats") or list_item.get("stats")),
                source_refs=source_refs,
                cross_refs=cross_refs,
            ),
        )

    return posts


def opus_posts_from_parsed(parsed_opus: list[dict[str, Any]]) -> list[ContentPost]:
    """Derive ContentPost from already-parsed OpusPost dicts (legacy model output).

    Single source of truth for opus→ContentPost mapping when reading from the
    parsing store.  Mirrors :func:`select_opus_posts` but consumes
    ``OpusPost.to_dict()`` output instead of raw fetching payloads.
    """
    posts: list[ContentPost] = []
    for opus in parsed_opus:
        if not isinstance(opus, dict):
            continue
        opus_id = str_or_empty(opus.get("id") or opus.get("opus_id"))
        if not opus_id:
            continue

        refs = CrossRefs.from_dict(opus.get("_cross_refs") or opus.get("cross_refs"))
        if not refs.opus_id:
            refs.opus_id = opus_id

        images: list[str] = []
        for image in opus.get("detail_images", []) or []:
            if isinstance(image, dict) and image.get("url"):
                images.append(str(image["url"]))
        images.extend(str(url) for url in opus.get("list_images", []) or [] if url)
        if opus.get("cover"):
            images.append(str(opus["cover"]))
        images = dedup_strings(*images)

        source_refs = dedup_source_refs(
            [SourceRef("opus", opus_id)]
            + [
                SourceRef.from_dict(ref)
                for ref in (opus.get("_source_refs") or opus.get("source_refs") or [])
                if isinstance(ref, SourceRef | dict)
            ]
        )

        posts.append(
            ContentPost(
                content_key=content_key_for_refs(refs),
                kind="opus",
                title=str_or_empty(opus.get("title", "")),
                summary=str_or_empty(opus.get("summary", "")),
                text=str_or_empty(opus.get("markdown") or opus.get("summary") or ""),
                markdown=str_or_empty(opus.get("markdown", "")),
                images=images,
                pub_time=opus.get("pub_time") if opus.get("pub_time") is not None else opus.get("ctime"),
                stats=stats_dict(opus.get("stats", {}) or {}),
                source_refs=source_refs,
                cross_refs=refs,
            ),
        )
    return posts
