"""Video-domain bilibili-api wrappers for fetching."""

from __future__ import annotations

import asyncio
from typing import Any

from bilibili_api import Credential
from bilibili_api.video import Video

from .._adapter_core import (
    extract_list_items as _extract_list_items,
)
from .._adapter_core import (
    extract_total_count as _extract_total_count,
)
from .._adapter_core import (
    json_safe as _json_safe,
)
from .._adapter_core import (
    map_bilibili_errors as _map_bilibili_errors,
)
from .._adapter_core import (
    normalise_api_result as _normalise_api_result,
)


def _extract_bvids_from_videos(raw_payload: dict) -> list[str]:
    """Extract all bvids from videos endpoint raw_payload (pages shape)."""
    bvids: list[str] = []
    for page in raw_payload.get("pages", []):
        vlist = page.get("list", {}).get("vlist", [])
        for item in vlist:
            bvid = item.get("bvid")
            if bvid:
                bvids.append(bvid)
    return bvids


async def fetch_video_detail_item(
    bvid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch get_info + get_tags for a single bvid."""
    v = Video(bvid, credential=credential)
    async with _map_bilibili_errors(f"video_detail[{bvid}]: get_info"):
        info = await asyncio.wait_for(v.get_info(), timeout=timeout)

    async with _map_bilibili_errors(f"video_detail[{bvid}]: get_tags"):
        tags = await asyncio.wait_for(v.get_tags(), timeout=timeout)

    return {"info": info, "tags": tags}


async def _video_pages(
    v: Video,
    bvid: str,
    timeout: float,
) -> list[dict[str, Any]]:
    async with _map_bilibili_errors(f"video[{bvid}]: get_pages"):
        pages = await asyncio.wait_for(v.get_pages(), timeout=timeout)
    return _json_safe(pages)


def _video_item_method(
    method_name: str,
    *,
    per_page: bool = False,
    page_arg: str = "cid",
    result_key: str | None = None,
    default_kwargs: dict[str, Any] | None = None,
):
    """Build a per-bvid fan-out callable for Video read methods."""

    async def _fn(
        bvid: str,
        credential: Credential | None,
        timeout: float = 30.0,
        **_kw: Any,
    ) -> dict[str, Any]:
        v = Video(bvid, credential=credential)
        key = result_key or method_name
        kwargs = dict(default_kwargs or {})

        if not per_page:
            async with _map_bilibili_errors(f"{key}[{bvid}]"):
                result = await asyncio.wait_for(
                    getattr(v, method_name)(**kwargs),
                    timeout=timeout,
                )
            return {key: _json_safe(result)}

        pages = await _video_pages(v, bvid, timeout)
        rows: list[dict[str, Any]] = []
        for idx, page in enumerate(pages):
            call_kwargs = dict(kwargs)
            cid = page.get("cid") if isinstance(page, dict) else None
            if page_arg == "cid":
                call_kwargs["cid"] = cid
            elif page_arg == "page_index":
                call_kwargs["page_index"] = idx
            elif page_arg == "both":
                call_kwargs["cid"] = cid
                call_kwargs["page_index"] = idx
            async with _map_bilibili_errors(f"{key}[{bvid}][{idx}]"):
                result = await asyncio.wait_for(
                    getattr(v, method_name)(**call_kwargs),
                    timeout=timeout,
                )
            rows.append(
                {
                    "page_index": idx,
                    "cid": cid,
                    "part": page.get("part", "") if isinstance(page, dict) else "",
                    "result": _json_safe(result),
                }
            )
        return {"pages": pages, key: rows}

    return _fn


fetch_video_pages_item = _video_item_method("get_pages", result_key="pages")
fetch_video_detail_full_item = _video_item_method("get_detail", result_key="detail")
fetch_video_ai_conclusion_item = _video_item_method(
    "get_ai_conclusion",
    per_page=True,
    page_arg="both",
    result_key="ai_conclusion",
)
fetch_video_danmaku_snapshot_item = _video_item_method(
    "get_danmaku_snapshot",
    result_key="danmaku_snapshot",
)
fetch_video_danmaku_view_item = _video_item_method(
    "get_danmaku_view",
    per_page=True,
    page_arg="both",
    result_key="danmaku_view",
)
fetch_video_danmaku_xml_item = _video_item_method(
    "get_danmaku_xml",
    per_page=True,
    page_arg="both",
    result_key="danmaku_xml",
)
fetch_video_danmakus_item = _video_item_method(
    "get_danmakus",
    per_page=True,
    page_arg="both",
    result_key="danmakus",
)
fetch_video_online_item = _video_item_method(
    "get_online",
    per_page=True,
    page_arg="both",
    result_key="online",
)
fetch_video_pay_coins_item = _video_item_method("get_pay_coins", result_key="pay_coins")
fetch_video_pbp_item = _video_item_method(
    "get_pbp",
    per_page=True,
    page_arg="both",
    result_key="pbp",
)
fetch_video_player_info_item = _video_item_method(
    "get_player_info",
    per_page=True,
    page_arg="cid",
    result_key="player_info",
)
fetch_video_private_notes_item = _video_item_method(
    "get_private_notes_list",
    result_key="private_notes",
)
fetch_video_related_item = _video_item_method("get_related", result_key="related")
fetch_video_relation_item = _video_item_method("get_relation", result_key="relation")
fetch_video_special_dms_item = _video_item_method(
    "get_special_dms",
    per_page=True,
    page_arg="both",
    result_key="special_dms",
)
fetch_video_up_mid_item = _video_item_method("get_up_mid", result_key="up_mid")
fetch_video_snapshot_item = _video_item_method(
    "get_video_snapshot",
    per_page=True,
    page_arg="cid",
    result_key="video_snapshot",
    default_kwargs={"json_index": True, "pvideo": False},
)
fetch_video_download_url_item = _video_item_method(
    "get_download_url",
    per_page=True,
    page_arg="both",
    result_key="download_url",
)
fetch_video_is_episode_item = _video_item_method("is_episode", result_key="is_episode")
fetch_video_is_forbid_note_item = _video_item_method(
    "is_forbid_note",
    result_key="is_forbid_note",
)
fetch_video_chargers_item = _video_item_method("get_chargers", result_key="chargers")


async def fetch_video_public_notes_item(
    bvid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    ps: int = 50,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch all public note pages for a bvid."""
    v = Video(bvid, credential=credential)
    pages: list[dict[str, Any]] = []
    pn = 1
    while True:
        async with _map_bilibili_errors(f"public_notes[{bvid}][{pn}]"):
            data = await asyncio.wait_for(
                v.get_public_notes_list(pn=pn, ps=ps),
                timeout=timeout,
            )
        safe = _normalise_api_result(data)
        pages.append(safe)
        items = _extract_list_items(safe)
        total = _extract_total_count(safe)
        if not items or (total > 0 and pn * ps >= total) or (total == 0 and len(items) < ps):
            break
        pn += 1
    return {"pages": pages}


__all__ = [
    "_extract_bvids_from_videos",
    "_video_pages",
    "fetch_video_ai_conclusion_item",
    "fetch_video_chargers_item",
    "fetch_video_danmaku_snapshot_item",
    "fetch_video_danmaku_view_item",
    "fetch_video_danmaku_xml_item",
    "fetch_video_danmakus_item",
    "fetch_video_detail_full_item",
    "fetch_video_detail_item",
    "fetch_video_download_url_item",
    "fetch_video_is_episode_item",
    "fetch_video_is_forbid_note_item",
    "fetch_video_online_item",
    "fetch_video_pages_item",
    "fetch_video_pay_coins_item",
    "fetch_video_pbp_item",
    "fetch_video_player_info_item",
    "fetch_video_private_notes_item",
    "fetch_video_related_item",
    "fetch_video_relation_item",
    "fetch_video_snapshot_item",
    "fetch_video_special_dms_item",
    "fetch_video_up_mid_item",
    "fetch_video_public_notes_item",
]
