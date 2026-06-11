# transform/dynamics — 动态内容 transform handler.
#
# Per docs/design/processing.md §6.3:
#   输入: dynamics 的 raw_payload，结构 {pages: [{items: [...], ...}]}
#   输出: 每条动态一个结构化 dict
#
# §19 已决：item_id == raw_payload.items[*].id_str。
#
# 真实数据形态（从 uid 13991807 抓取结果实测确认）：
#   modules 是 dict，不是 list；包含
#       module_author     {pub_ts: "<epoch_seconds_string>", name, mid, ...}
#       module_dynamic    {desc: {text: "..."} | None,
#                          major: {type: "MAJOR_TYPE_*", archive|article|draw|...},
#                          additional, topic}
#       module_stat       {forward, comment, like, coin, favorite}
#   FORWARD 类型把原动态副本放在顶层 `orig` 字段。
#
# 已支持类型：DYNAMIC_TYPE_WORD / DRAW / AV / ARTICLE / FORWARD / COMMON_SQUARE。
# 未识别的 type 仍会产出工作项，但 result.text/major 可能为空。

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "dynamics"
SOURCE_ENDPOINTS: tuple[str, ...] = ("dynamics",)


def _modules_dict(d: dict) -> dict:
    """Return the modules dict; tolerate the rare list-of-dicts shape."""
    mods = d.get("modules")
    if isinstance(mods, dict):
        return mods
    if isinstance(mods, list):
        merged: dict = {}
        for m in mods:
            if isinstance(m, dict):
                merged.update(m)
        return merged
    return {}


def _extract_pub_ts(modules: dict) -> int | None:
    """Pub timestamp lives at module_author.pub_ts (string epoch_seconds)."""
    author = modules.get("module_author") if modules else None
    if isinstance(author, dict):
        ts = author.get("pub_ts")
        if isinstance(ts, str) and ts.isdigit():
            return int(ts)
        if isinstance(ts, int):
            return ts
    return None


def _extract_desc_text(modules: dict) -> str:
    """Top-level dynamic text, from module_dynamic.desc.text."""
    md = modules.get("module_dynamic") if modules else None
    if not isinstance(md, dict):
        return ""
    desc = md.get("desc")
    if isinstance(desc, dict):
        txt = desc.get("text")
        if isinstance(txt, str):
            return txt
    return ""


def _extract_major(modules: dict) -> dict[str, Any]:
    """Pull the type-specific 'major' block into a flat structured form.

    Returns a dict with at least {"type": "..."}.  Known shapes:
      MAJOR_TYPE_ARCHIVE  → {bvid, aid, title, desc, duration_text, jump_url, cover}
      MAJOR_TYPE_ARTICLE  → {id, title, desc, jump_url, covers}
      MAJOR_TYPE_DRAW     → {images: [src, ...]}
      MAJOR_TYPE_OPUS     → {summary_text, pics: [url, ...]}
      MAJOR_TYPE_LIVE_RCMD/...  → ignored except for type tag
    """
    md = modules.get("module_dynamic") if modules else None
    if not isinstance(md, dict):
        return {}
    major = md.get("major")
    if not isinstance(major, dict):
        return {}
    mtype = major.get("type") or ""
    out: dict[str, Any] = {"type": mtype}

    if mtype == "MAJOR_TYPE_ARCHIVE":
        arc = major.get("archive") or {}
        if isinstance(arc, dict):
            out.update({
                "bvid": arc.get("bvid"),
                "aid": arc.get("aid"),
                "title": arc.get("title") or "",
                "desc": arc.get("desc") or "",
                "duration_text": arc.get("duration_text") or "",
                "jump_url": arc.get("jump_url") or "",
                "cover": arc.get("cover") or "",
            })
    elif mtype == "MAJOR_TYPE_ARTICLE":
        art = major.get("article") or {}
        if isinstance(art, dict):
            out.update({
                "id": art.get("id"),
                "title": art.get("title") or "",
                "desc": art.get("desc") or "",
                "jump_url": art.get("jump_url") or "",
                "covers": art.get("covers") or [],
            })
    elif mtype == "MAJOR_TYPE_DRAW":
        draw = major.get("draw") or {}
        images: list[str] = []
        if isinstance(draw, dict):
            for it in draw.get("items", []) or []:
                if isinstance(it, dict):
                    src = it.get("src")
                    if isinstance(src, str) and src:
                        images.append(src)
        out["images"] = images
    elif mtype == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            summary = opus.get("summary") or {}
            text = ""
            if isinstance(summary, dict):
                text = summary.get("text") or ""
            pics_in = opus.get("pics") or []
            pics: list[str] = []
            if isinstance(pics_in, list):
                for p in pics_in:
                    if isinstance(p, dict):
                        url = p.get("url")
                        if isinstance(url, str) and url:
                            pics.append(url)
            out["summary_text"] = text
            out["pics"] = pics
    # Other major types (LIVE_RCMD / COMMON / MUSIC / PGC / ...) keep just `type`.

    return out


def _flatten_dynamic(d: dict) -> dict[str, Any]:
    """Flatten one dynamic (or `orig` sub-dynamic) into a result dict."""
    modules = _modules_dict(d)
    return {
        "id_str": d.get("id_str") or "",
        "type": d.get("type") or "",
        "text": _extract_desc_text(modules),
        "timestamp": _extract_pub_ts(modules),
        "major": _extract_major(modules),
    }


class _DynamicsHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        rp = raw_payloads.get("dynamics") or {}
        items: list[WorkItem] = []
        for page in rp.get("pages", []) or []:
            if not isinstance(page, dict):
                continue
            for d in page.get("items", []) or []:
                if not isinstance(d, dict):
                    continue
                id_str = d.get("id_str")
                if not isinstance(id_str, str) or not id_str:
                    continue
                items.append(WorkItem(item_type=ITEM_TYPE, item_id=id_str, item_data=d))
        return items

    def transform(self, item: WorkItem) -> dict[str, Any]:
        d = item.item_data
        flat = _flatten_dynamic(d)

        # FORWARD: include the original dynamic flattened under `forwarded`.
        orig = d.get("orig")
        forwarded: dict[str, Any] | None = None
        if isinstance(orig, dict):
            forwarded = _flatten_dynamic(orig)

        return {
            "id_str": flat["id_str"] or item.item_id,
            "type": flat["type"],
            "text": flat["text"],
            "timestamp": flat["timestamp"],
            "major": flat["major"],
            "forwarded": forwarded,
        }


HANDLER: TransformHandler = _DynamicsHandler()
