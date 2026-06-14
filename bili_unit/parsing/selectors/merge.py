from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bili_unit.parsing.models.content_post import (
    ContentPost,
    CrossRefs,
    content_key_for_refs,
)

from ._common import dedup_source_refs, dedup_strings


def _as_post(value: ContentPost | dict[str, Any]) -> ContentPost:
    if isinstance(value, ContentPost):
        return value
    return ContentPost.from_dict(value)


def _merge_refs(base: CrossRefs, incoming: CrossRefs) -> CrossRefs:
    return CrossRefs(
        cvid=base.cvid or incoming.cvid,
        opus_id=base.opus_id or incoming.opus_id,
        dynamic_id=base.dynamic_id or incoming.dynamic_id,
        bvid=base.bvid or incoming.bvid,
    )


def _is_detail_source(post: ContentPost) -> bool:
    return any(ref.endpoint in {"article_detail", "opus_detail"} for ref in post.source_refs)


def _prefer_text(current: str, incoming: str, incoming_is_detail: bool) -> str:
    if incoming and (incoming_is_detail or not current):
        return incoming
    return current


def _priority_key(post: ContentPost) -> str:
    return content_key_for_refs(post.cross_refs, post.content_key)


def _aliases_for(post: ContentPost) -> set[str]:
    aliases: set[str] = set()
    if post.content_key:
        aliases.add(post.content_key)
    if post.cross_refs.cvid:
        aliases.add(f"article:{post.cross_refs.cvid}")
    if post.cross_refs.opus_id:
        aliases.add(f"opus:{post.cross_refs.opus_id}")
    if post.cross_refs.bvid:
        aliases.add(f"video:{post.cross_refs.bvid}")
    if post.cross_refs.dynamic_id:
        aliases.add(f"dynamic:{post.cross_refs.dynamic_id}")
    return aliases


def _fresh_from(post: ContentPost, key: str) -> ContentPost:
    return ContentPost(
        content_key=key,
        kind=post.kind,
        title=post.title,
        summary=post.summary,
        text=post.text,
        markdown=post.markdown,
        images=list(post.images),
        pub_time=post.pub_time,
        stats=dict(post.stats),
        source_refs=list(post.source_refs),
        cross_refs=post.cross_refs,
    )


def _merge_into(base: ContentPost, incoming: ContentPost) -> None:
    incoming_is_detail = _is_detail_source(incoming)
    base.source_refs = dedup_source_refs([*base.source_refs, *incoming.source_refs])
    base.cross_refs = _merge_refs(base.cross_refs, incoming.cross_refs)
    base.content_key = content_key_for_refs(base.cross_refs, base.content_key)

    if incoming.kind and (
        incoming.kind in {"article", "opus", "video"} and base.kind.startswith("dynamic")
        or not base.kind
    ):
        base.kind = incoming.kind

    if incoming.title and (incoming_is_detail or not base.title):
        base.title = incoming.title
    if incoming.summary and (incoming_is_detail or not base.summary):
        base.summary = incoming.summary
    base.text = _prefer_text(base.text, incoming.text, incoming_is_detail)
    base.markdown = _prefer_text(base.markdown, incoming.markdown, incoming_is_detail)
    base.images = dedup_strings(base.images, incoming.images)
    if base.pub_time is None:
        base.pub_time = incoming.pub_time
    if incoming.stats:
        base.stats = {**incoming.stats, **base.stats}


def merge_content_posts(posts: Iterable[ContentPost | dict[str, Any]]) -> list[ContentPost]:
    merged: dict[str, ContentPost] = {}
    aliases: dict[str, str] = {}

    for value in posts:
        incoming = _as_post(value)
        key = _priority_key(incoming)
        if not key:
            continue

        incoming_aliases = _aliases_for(incoming) | {key}
        hit_keys: list[str] = []
        for alias in incoming_aliases:
            hit_key = aliases.get(alias)
            if hit_key in merged and hit_key not in hit_keys:
                hit_keys.append(hit_key)

        if not hit_keys:
            canonical = key
            merged[canonical] = _fresh_from(incoming, canonical)
            all_aliases = incoming_aliases
        else:
            old_keys = set(hit_keys)
            canonical = hit_keys[0]
            base = merged.pop(canonical)
            all_aliases = _aliases_for(base) | incoming_aliases | old_keys

            for other_key in hit_keys[1:]:
                other = merged.pop(other_key)
                all_aliases |= _aliases_for(other)
                _merge_into(base, other)

            _merge_into(base, incoming)
            canonical = content_key_for_refs(base.cross_refs, base.content_key)
            base.content_key = canonical
            merged[canonical] = base
            all_aliases |= _aliases_for(base) | {canonical}

        for alias in all_aliases:
            aliases[alias] = canonical

    return sorted(
        merged.values(),
        key=lambda post: (
            0 if post.content_key.startswith("article:")
            else 1 if post.content_key.startswith("opus:")
            else 2 if post.content_key.startswith("video:")
            else 3,
            post.content_key,
        ),
    )


def merge_posts(posts: Iterable[ContentPost | dict[str, Any]]) -> list[ContentPost]:
    return merge_content_posts(posts)
