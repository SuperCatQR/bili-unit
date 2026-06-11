# transform/opus — 图文 transform handler.
#
# Per docs/feature/processing.md:
#   输入: opus 的 raw_payload，结构 {pages: [{items: [...], offset, has_more}]}
#         + opus_detail 的 per-opus_id raw_payload，结构 {info, markdown, images}
#   输出: 每个 opus 一个结构化 dict（列表级字段 + 正文 markdown + 图片清单）
#
# opus_detail 是可选 enrichment endpoint：
#   - 若某 opus_id 拉到了 opus_detail.SUCCESS，正文 markdown / images / word_count
#     写入 result。
#   - 拉不到（删除、私密、fetching 还没跑过 opus_detail）时降级回列表级字段，
#     与 opus_detail 接入前的旧行为兼容。
#
# 与 transform/articles 平行；两者 *不* 去重 —— is_article() 为 true 的 opus
# 会同时出现在 articles + opus 两条 item_type 里，下游若需合并自己处理。

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "opus"
SOURCE_ENDPOINTS: tuple[str, ...] = ("opus", "opus_detail")
OPTIONAL_ENDPOINTS: tuple[str, ...] = ("opus_detail",)


def _str_or_empty(v: Any) -> str:
    return v if isinstance(v, str) else ""


class _OpusHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    optional_endpoints = OPTIONAL_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        """Build one WorkItem per opus from the listing payload.

        ``opus_detail`` is attached at transform-time, not item-time:
        the runner pre-collected ``{opus_id → detail_payload}`` and stashes it
        under ``raw_payloads["opus_detail"]``.  ``transform`` looks it up
        per-item; missing entries fall back to list-level fields.
        """
        rp = raw_payloads.get("opus") or {}
        details = raw_payloads.get("opus_detail") or {}
        items: list[WorkItem] = []
        for page in rp.get("pages", []) or []:
            if not isinstance(page, dict):
                continue
            for it in page.get("items", []) or []:
                if not isinstance(it, dict):
                    continue
                oid = it.get("opus_id")
                if oid is None:
                    continue
                opus_id_str = str(oid)
                items.append(WorkItem(
                    item_type=ITEM_TYPE,
                    item_id=opus_id_str,
                    item_data={
                        "list": it,
                        "detail": details.get(opus_id_str),
                    },
                ))
        return items

    def transform(self, item: WorkItem) -> dict[str, Any]:
        data = item.item_data
        # extract_items always wraps as {"list": ..., "detail": ...}, but
        # tolerate the legacy bare-dict shape for callers that hand-craft
        # WorkItems in tests.
        if isinstance(data, dict) and "list" in data and "detail" in data:
            opus = data.get("list") or {}
            detail = data.get("detail")
        else:
            opus = data
            detail = None

        # ---- list-level fields ----
        # Opus list items have ``module_dynamic.major.opus`` as the body
        # summary (same shape as a dynamic ``MAJOR_TYPE_OPUS``), with the
        # surrounding modules carrying author/stats.  We tolerate either
        # the modules-list / modules-dict shape and the rare flat shape.
        modules = opus.get("modules")
        if isinstance(modules, list):
            merged: dict[str, Any] = {}
            for m in modules:
                if isinstance(m, dict):
                    merged.update(m)
            modules = merged
        elif not isinstance(modules, dict):
            modules = {}

        # Cover / first image: prefer the list-level ``cover`` field; else
        # peek at ``module_dynamic.major.opus.pics[*].url``.
        cover_url = _str_or_empty(opus.get("cover"))

        # title — list items expose ``title``; some shapes only expose it
        # under modules.module_top, but list-level coverage is enough here.
        title = _str_or_empty(opus.get("title"))

        # summary — list items expose ``summary`` plain text; fall back to
        # the dynamic-style summary text under modules.
        summary = _str_or_empty(opus.get("summary"))
        if not summary:
            md = modules.get("module_dynamic") if modules else None
            if isinstance(md, dict):
                major = md.get("major")
                if isinstance(major, dict):
                    o = major.get("opus") or {}
                    if isinstance(o, dict):
                        s = o.get("summary") or {}
                        if isinstance(s, dict):
                            summary = _str_or_empty(s.get("text"))

        # image list from list-level body
        list_images: list[str] = []
        md = modules.get("module_dynamic") if modules else None
        if isinstance(md, dict):
            major = md.get("major")
            if isinstance(major, dict):
                o = major.get("opus") or {}
                if isinstance(o, dict):
                    for p in o.get("pics") or []:
                        if isinstance(p, dict):
                            url = p.get("url")
                            if isinstance(url, str) and url:
                                list_images.append(url)

        stats_in = opus.get("stats")
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

        # publish time — list items use ``pub_time`` (epoch seconds int) or
        # ``ctime``.  Both are accepted; first available wins.
        ctime = opus.get("pub_time")
        if ctime is None:
            ctime = opus.get("ctime")

        # jump_url — useful for external links / debugging.
        jump_url = _str_or_empty(opus.get("jump_url"))

        # ---- detail enrichment ----
        # detail.markdown is the rendered body via Opus.markdown(); detail.images
        # is the raw image-info list (URL, width, height, ...) from
        # Opus.get_images_raw_info() — keep it as-is so callers can render
        # responsive sizes without re-walking modules.
        markdown_text: str = ""
        detail_images: list[dict[str, Any]] = []
        if isinstance(detail, dict):
            md_text = detail.get("markdown")
            if isinstance(md_text, str):
                markdown_text = md_text
            di = detail.get("images")
            if isinstance(di, list):
                detail_images = [d for d in di if isinstance(d, dict)]

        # image_urls: prefer the detail image list (richer + canonical) when
        # available, else fall back to the list-level pics.
        if detail_images:
            image_urls: list[str] = []
            for img in detail_images:
                u = img.get("url")
                if isinstance(u, str) and u:
                    image_urls.append(u)
        else:
            image_urls = list(list_images)

        if cover_url and cover_url not in image_urls:
            # cover sometimes duplicates the first body image; only prepend
            # when distinct so callers can rely on image_urls[0] == cover.
            image_urls.insert(0, cover_url)

        return {
            "id": item.item_id,
            "title": title,
            "summary": summary,
            "image_urls": image_urls,
            "stats": stats_out,
            "ctime": ctime,
            "jump_url": jump_url,
            "markdown": markdown_text,
            "images": detail_images,
            "word_count": len(markdown_text),
        }


HANDLER: TransformHandler = _OpusHandler()
