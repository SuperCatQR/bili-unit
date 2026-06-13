from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    str_or_none,
)

_OPUS_URL_RE = re.compile(r"/opus/(\d+)")


@dataclass
class DynamicEvent:
    dynamic_id: str = ""
    type: str = ""
    text: str = ""
    pub_time: int | None = None
    major_type: str = ""
    target_ref: str = ""
    forwarded_ref: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    @property
    def item_id(self) -> str:
        return self.dynamic_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "dynamic_id": self.dynamic_id,
            "type": self.type,
            "text": self.text,
            "pub_time": self.pub_time,
            "major_type": self.major_type,
            "target_ref": self.target_ref,
            "forwarded_ref": self.forwarded_ref,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DynamicEvent:
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in value.get("source_refs", []) or []
            if isinstance(ref, SourceRef | dict)
        ]
        return cls(
            dynamic_id=str_or_empty(value.get("dynamic_id")),
            type=str_or_empty(value.get("type")),
            text=str_or_empty(value.get("text")),
            pub_time=value.get("pub_time"),
            major_type=str_or_empty(value.get("major_type")),
            target_ref=str_or_empty(value.get("target_ref")),
            forwarded_ref=str_or_none(value.get("forwarded_ref")),
            source_refs=source_refs,
            cross_refs=CrossRefs.from_dict(value.get("cross_refs")),
        )


def _dynamic_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    return pages_items(payload, "items")


def _modules(raw: dict[str, Any]) -> dict[str, Any]:
    return module_map(raw.get("modules"))


def _module_dynamic(raw: dict[str, Any]) -> dict[str, Any]:
    return dict_or_empty(_modules(raw).get("module_dynamic"))


def _major(raw: dict[str, Any]) -> dict[str, Any]:
    return dict_or_empty(_module_dynamic(raw).get("major"))


def _desc_text(raw: dict[str, Any]) -> str:
    desc = dict_or_empty(_module_dynamic(raw).get("desc"))
    return str_or_empty(desc.get("text"))


def _pub_time(raw: dict[str, Any]) -> int | None:
    author = dict_or_empty(_modules(raw).get("module_author"))
    return int_or_none(author.get("pub_ts") or raw.get("pub_time") or raw.get("ctime"))


def _dynamic_id(raw: dict[str, Any]) -> str:
    return str_or_empty(raw.get("id_str") or raw.get("dynamic_id") or raw.get("id"))


def _major_type(major: dict[str, Any]) -> str:
    return str_or_empty(major.get("type"))


def _images_from_pics(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        url
        for item in value
        if isinstance(item, dict)
        for url in [str_or_empty(item.get("url") or item.get("src"))]
        if url
    ]


def _images_from_draw(draw: dict[str, Any]) -> list[str]:
    items = draw.get("items")
    if not isinstance(items, list):
        return []
    return [
        url
        for item in items
        if isinstance(item, dict)
        for url in [str_or_empty(item.get("src") or item.get("url"))]
        if url
    ]


def _opus_id_from(raw: dict[str, Any]) -> str:
    opus_id = str_or_empty(raw.get("opus_id") or raw.get("id"))
    if opus_id:
        return opus_id
    jump_url = str_or_empty(raw.get("jump_url"))
    match = _OPUS_URL_RE.search(jump_url)
    return match.group(1) if match else ""


def _summary_text(opus: dict[str, Any]) -> str:
    summary = dict_or_empty(opus.get("summary"))
    return str_or_empty(summary.get("text"))


def _article_post(dynamic_id: str, text: str, pub_time: int | None, major: dict[str, Any]) -> ContentPost | None:
    article = dict_or_empty(major.get("article"))
    cvid = str_or_empty(article.get("id") or article.get("cvid") or article.get("article_id"))
    if not cvid:
        return None

    cross_refs = CrossRefs(cvid=cvid, dynamic_id=dynamic_id)
    return ContentPost(
        content_key=content_key_for_refs(cross_refs),
        kind="article",
        title=str_or_empty(article.get("title")),
        summary=str_or_empty(article.get("desc") or text),
        text=text,
        markdown="",
        images=dedup_strings(article.get("covers"), article.get("cover")),
        pub_time=pub_time,
        stats=stats_dict(article.get("stats")),
        source_refs=dedup_source_refs([SourceRef("dynamics", dynamic_id)]),
        cross_refs=cross_refs,
    )


def _opus_post(dynamic_id: str, text: str, pub_time: int | None, major: dict[str, Any]) -> ContentPost | None:
    opus = dict_or_empty(major.get("opus"))
    opus_id = _opus_id_from(opus)
    if not opus_id:
        return None

    cross_refs = CrossRefs(opus_id=opus_id, dynamic_id=dynamic_id)
    return ContentPost(
        content_key=content_key_for_refs(cross_refs),
        kind="opus",
        title=str_or_empty(opus.get("title")),
        summary=_summary_text(opus) or text,
        text=_summary_text(opus) or text,
        markdown="",
        images=dedup_strings(_images_from_pics(opus.get("pics")), opus.get("cover")),
        pub_time=pub_time,
        stats=stats_dict(opus.get("stats")),
        source_refs=dedup_source_refs([SourceRef("dynamics", dynamic_id)]),
        cross_refs=cross_refs,
    )


def _archive_post(dynamic_id: str, text: str, pub_time: int | None, major: dict[str, Any]) -> ContentPost | None:
    archive = dict_or_empty(major.get("archive"))
    bvid = str_or_empty(archive.get("bvid"))
    if not bvid:
        return None

    cross_refs = CrossRefs(dynamic_id=dynamic_id, bvid=bvid)
    return ContentPost(
        content_key=content_key_for_refs(cross_refs),
        kind="video",
        title=str_or_empty(archive.get("title")),
        summary=str_or_empty(archive.get("desc") or text),
        text=text,
        markdown="",
        images=dedup_strings(archive.get("cover")),
        pub_time=pub_time,
        stats=stats_dict(archive.get("stat") or archive.get("stats")),
        source_refs=dedup_source_refs([SourceRef("dynamics", dynamic_id)]),
        cross_refs=cross_refs,
    )


def _draw_post(dynamic_id: str, text: str, pub_time: int | None, major: dict[str, Any]) -> ContentPost | None:
    draw = dict_or_empty(major.get("draw"))
    cross_refs = CrossRefs(dynamic_id=dynamic_id)
    return ContentPost(
        content_key=content_key_for_refs(cross_refs),
        kind="dynamic_draw",
        title="",
        summary=text,
        text=text,
        markdown="",
        images=_images_from_draw(draw),
        pub_time=pub_time,
        stats=stats_dict(draw.get("stats")),
        source_refs=dedup_source_refs([SourceRef("dynamics", dynamic_id)]),
        cross_refs=cross_refs,
    )


def _post_from_major(
    dynamic_id: str,
    text: str,
    pub_time: int | None,
    major: dict[str, Any],
) -> ContentPost | None:
    match _major_type(major):
        case "MAJOR_TYPE_ARTICLE":
            return _article_post(dynamic_id, text, pub_time, major)
        case "MAJOR_TYPE_OPUS":
            return _opus_post(dynamic_id, text, pub_time, major)
        case "MAJOR_TYPE_ARCHIVE":
            return None
        case "MAJOR_TYPE_DRAW":
            return _draw_post(dynamic_id, text, pub_time, major)
        case _:
            return None


def _target_for_major(dynamic_id: str, major: dict[str, Any]) -> tuple[str, CrossRefs]:
    major_type = _major_type(major)
    if major_type == "MAJOR_TYPE_ARTICLE":
        article = dict_or_empty(major.get("article"))
        cvid = str_or_empty(article.get("id") or article.get("cvid") or article.get("article_id"))
        refs = CrossRefs(cvid=str_or_none(cvid), dynamic_id=str_or_none(dynamic_id))
        return (content_key_for_refs(refs), refs) if cvid else ("", refs)

    if major_type == "MAJOR_TYPE_OPUS":
        opus_id = _opus_id_from(dict_or_empty(major.get("opus")))
        refs = CrossRefs(opus_id=str_or_none(opus_id), dynamic_id=str_or_none(dynamic_id))
        return (content_key_for_refs(refs), refs) if opus_id else ("", refs)

    if major_type == "MAJOR_TYPE_ARCHIVE":
        archive = dict_or_empty(major.get("archive"))
        bvid = str_or_empty(archive.get("bvid"))
        refs = CrossRefs(dynamic_id=str_or_none(dynamic_id), bvid=str_or_none(bvid))
        return (f"video:{bvid}", refs) if bvid else ("", refs)

    if major_type == "MAJOR_TYPE_DRAW" and dynamic_id:
        refs = CrossRefs(dynamic_id=dynamic_id)
        return content_key_for_refs(refs), refs

    return "", CrossRefs(dynamic_id=str_or_none(dynamic_id))


def _event_from_raw(raw: dict[str, Any]) -> DynamicEvent | None:
    dynamic_id = _dynamic_id(raw)
    if not dynamic_id:
        return None

    dynamic_type = str_or_empty(raw.get("type"))
    text = _desc_text(raw)
    pub_time = _pub_time(raw)
    major = _major(raw)
    target_ref, cross_refs = _target_for_major(dynamic_id, major)
    forwarded_ref: str | None = None

    orig = raw.get("orig")
    if isinstance(orig, dict):
        orig_id = _dynamic_id(orig)
        if orig_id:
            forwarded_ref = f"dynamic:{orig_id}"
        orig_target_ref, orig_cross_refs = _target_for_major(orig_id, _major(orig))
        if orig_target_ref:
            target_ref = orig_target_ref
        cross_refs = cross_refs.merge_missing(orig_cross_refs)

    return DynamicEvent(
        dynamic_id=dynamic_id,
        type=dynamic_type,
        text=text,
        pub_time=pub_time,
        major_type=_major_type(major),
        target_ref=target_ref,
        forwarded_ref=forwarded_ref,
        source_refs=dedup_source_refs([SourceRef("dynamics", dynamic_id)]),
        cross_refs=cross_refs,
    )


def select_dynamic_events(raw_dynamics_payload: dict[str, Any] | None) -> list[DynamicEvent]:
    events: list[DynamicEvent] = []
    for raw in _dynamic_items(raw_dynamics_payload):
        event = _event_from_raw(raw)
        if event is not None:
            events.append(event)
    return events


def _forward_post(raw: dict[str, Any], event: DynamicEvent) -> ContentPost | None:
    if not event.forwarded_ref and event.type != "DYNAMIC_TYPE_FORWARD":
        return None
    if not event.text:
        return None

    images: list[str] = []
    orig = raw.get("orig")
    if isinstance(orig, dict):
        orig_post = _post_from_major(_dynamic_id(orig), _desc_text(orig), _pub_time(orig), _major(orig))
        images = list(orig_post.images) if orig_post else []

    cross_refs = CrossRefs(
        cvid=event.cross_refs.cvid,
        opus_id=event.cross_refs.opus_id,
        dynamic_id=event.dynamic_id,
        bvid=event.cross_refs.bvid,
    )
    return ContentPost(
        content_key=content_key_for_refs(CrossRefs(dynamic_id=event.dynamic_id)),
        kind="forward",
        title="",
        summary=event.text,
        text=event.text,
        markdown="",
        images=images,
        pub_time=event.pub_time,
        stats={},
        source_refs=event.source_refs,
        cross_refs=cross_refs,
    )


def select_dynamic_content(raw_dynamics_payload: dict[str, Any] | None) -> list[ContentPost]:
    posts: list[ContentPost] = []

    for raw in _dynamic_items(raw_dynamics_payload):
        event = _event_from_raw(raw)
        if event is None:
            continue

        post = _post_from_major(event.dynamic_id, event.text, event.pub_time, _major(raw))
        if post is not None:
            posts.append(post)

        forward_post = _forward_post(raw, event)
        if forward_post is not None:
            posts.append(forward_post)

        orig = raw.get("orig")
        if isinstance(orig, dict):
            orig_id = _dynamic_id(orig)
            orig_post = _post_from_major(orig_id, _desc_text(orig), _pub_time(orig), _major(orig))
            if orig_post is not None:
                refs = dedup_source_refs([*event.source_refs, *orig_post.source_refs])
                orig_post.source_refs = refs
                if event.dynamic_id:
                    orig_post.cross_refs = orig_post.cross_refs.merge_missing(CrossRefs(dynamic_id=event.dynamic_id))
                posts.append(orig_post)

    return posts


def select_dynamic_posts(raw_dynamics_payload: dict[str, Any] | None) -> list[ContentPost]:
    return select_dynamic_content(raw_dynamics_payload)
