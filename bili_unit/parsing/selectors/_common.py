from __future__ import annotations

from typing import Any

from bili_unit.parsing.models.content_post import ContentPost, CrossRefs, SourceRef


def str_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def str_or_none(value: Any) -> str | None:
    text = str_or_empty(value)
    return text or None


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def pages_items(payload: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    items: list[dict[str, Any]] = []
    pages = payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            items.extend(list_of_dicts(page.get(field)))
        return items

    return list_of_dicts(payload.get(field))


def dedup_strings(*sources: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for source in sources:
        if isinstance(source, str):
            values = [source]
        elif isinstance(source, list):
            values = source
        else:
            continue
        for value in values:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
    return result


def dedup_source_refs(refs: list[SourceRef]) -> list[SourceRef]:
    seen: set[tuple[str, str]] = set()
    result: list[SourceRef] = []
    for ref in refs:
        key = (ref.endpoint, ref.item_id)
        if not ref.endpoint or not ref.item_id or key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def module_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        return {}

    modules: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        module_type = str_or_empty(entry.get("module_type"))
        if module_type:
            modules[module_type] = entry
        else:
            modules.update(entry)
    return modules


def stats_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def detail_text_from_content_json(value: Any) -> str:
    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if node:
                parts.append(node)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        for key in ("text", "content", "raw_text"):
            raw = node.get(key)
            if isinstance(raw, str) and raw:
                parts.append(raw)
                break
        for key in ("children", "items"):
            visit(node.get(key))

    visit(value)
    return "\n".join(parts)


def video_posts_from_parsed(items: list[dict]) -> list[ContentPost]:
    """Convert stored VideoDetail dicts → ContentPost candidates.

    Mirrors :func:`article_posts_from_parsed` / :func:`opus_posts_from_parsed`:
    consumes ``VideoDetail.to_dict()`` output and emits a ``ContentPost`` with
    ``kind="video"`` and ``content_key=f"video:{bvid}"``. The video desc maps to
    both summary and text since the legacy ``VideoDetail`` does not carry a
    separate textual body.
    """
    out: list[ContentPost] = []
    for v in items:
        if not isinstance(v, dict):
            continue
        bvid = str_or_empty(v.get("bvid"))
        if not bvid:
            continue

        cross_refs = CrossRefs.from_dict(v.get("_cross_refs") or v.get("cross_refs"))
        if not cross_refs.bvid:
            cross_refs.bvid = bvid

        existing_refs = [
            SourceRef.from_dict(ref)
            for ref in (v.get("_source_refs") or v.get("source_refs") or [])
            if isinstance(ref, SourceRef | dict)
        ]
        source_refs = dedup_source_refs(
            [SourceRef("video_detail", bvid), *existing_refs]
        )

        desc = str_or_empty(v.get("desc"))
        pic = str_or_empty(v.get("pic"))
        pub_time = v.get("pubdate")
        if pub_time is None:
            pub_time = v.get("ctime")

        out.append(
            ContentPost(
                content_key=f"video:{bvid}",
                kind="video",
                title=str_or_empty(v.get("title")),
                summary=desc,
                text=desc,
                markdown="",
                images=[pic] if pic else [],
                pub_time=pub_time,
                stats=stats_dict(v.get("stat", {}) or {}),
                source_refs=source_refs,
                cross_refs=cross_refs,
            ),
        )
    return out
