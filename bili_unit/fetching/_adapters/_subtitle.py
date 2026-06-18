"""Subtitle-domain bilibili-api wrappers for fetching."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from bilibili_api import Credential
from bilibili_api.video import Video

from .._adapter_core import (
    json_safe as _json_safe,
)
from .._adapter_core import (
    map_bilibili_errors as _map_bilibili_errors,
)
from ._video import _video_pages

_SUBTITLE_FETCH_TIMEOUT = 10.0


def _normalise_subtitle_url(url: str) -> str:
    """Make a B 站 subtitle URL absolute (B 站常返回 ``//host/...`` 缺 scheme)."""
    if not url.startswith(("http://", "https://")):
        return "https:" + url
    return url


async def _fetch_subtitle_body(
    session: aiohttp.ClientSession,
    url: str,
) -> dict[str, Any]:
    """Fetch one subtitle JSON URL. Returns ``{"body": [...]}`` or
    ``{"_fetch_error": "<reason>"}`` — never raises."""
    try:
        normalised = _normalise_subtitle_url(url)
        async with session.get(normalised) as resp:
            if resp.status != 200:
                return {"_fetch_error": f"http {resp.status}"}
            data = await resp.json(content_type=None)
    except TimeoutError:
        return {"_fetch_error": "timeout"}
    except aiohttp.ClientError as exc:
        return {"_fetch_error": f"client error: {exc}"}
    except (ValueError, TypeError) as exc:
        return {"_fetch_error": f"json parse: {exc}"}

    if not isinstance(data, dict):
        return {"_fetch_error": "unexpected shape: not a dict"}
    body = data.get("body")
    if not isinstance(body, list):
        return {"_fetch_error": "unexpected shape: missing body"}
    return {"body": body}


async def _missing_url_error() -> dict[str, Any]:
    """Awaitable stub returned when a subtitles entry has no ``subtitle_url``."""
    return {"_fetch_error": "missing subtitle_url"}


async def fetch_video_subtitle_item(
    bvid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch subtitle index + body content for every page of a bvid.

    Per-page step 1 calls ``Video.get_subtitle(cid)`` (the index, with
    ``subtitles[*].subtitle_url / lan / lan_doc``); step 2 then GETs every
    URL and stuffs the parsed JSON ``body`` into a sibling ``content`` list.
    A single ``aiohttp.ClientSession`` is reused across all pages; ``content``
    items for the same page are fetched concurrently via ``asyncio.gather``,
    while different pages are walked sequentially to keep the connection
    fan-out small.

    Returned shape::

        {
          "pages": [...],
          "subtitle": [
            {
              "page_index": 0, "cid": ..., "part": "...",
              "result": {...},                      # raw get_subtitle output
              "content": [
                {"lan": "zh-CN", "lan_doc": "中文",
                 "body": [{"from": 0.0, "to": 3.5, "content": "..."}]},
                ...
              ]
            }, ...
          ]
        }

    A single lang URL failing (HTTP / parse / timeout) is recorded in-place
    on that lang entry as ``"_fetch_error": "<reason>"`` (without ``body``),
    leaving sibling langs and other pages unaffected.  An entirely-missing
    subtitle index for a page yields ``content: []``.
    """
    v = Video(bvid, credential=credential)
    pages = await _video_pages(v, bvid, timeout)

    rows: list[dict[str, Any]] = []
    client_timeout = aiohttp.ClientTimeout(total=_SUBTITLE_FETCH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        for idx, page in enumerate(pages):
            cid = page.get("cid") if isinstance(page, dict) else None
            part = page.get("part", "") if isinstance(page, dict) else ""

            async with _map_bilibili_errors(f"video_subtitle[{bvid}][{idx}]"):
                index = await asyncio.wait_for(
                    v.get_subtitle(cid=cid),
                    timeout=timeout,
                )
            index_safe = _json_safe(index)

            subtitles = []
            if isinstance(index_safe, dict):
                raw_subs = index_safe.get("subtitles")
                if isinstance(raw_subs, list):
                    subtitles = raw_subs

            if subtitles:
                tasks = [
                    _fetch_subtitle_body(session, sub.get("subtitle_url", ""))
                    if isinstance(sub, dict) and sub.get("subtitle_url")
                    else _missing_url_error()
                    for sub in subtitles
                ]
                fetched = await asyncio.gather(*tasks)
                content: list[dict[str, Any]] = []
                for sub, fetch_result in zip(subtitles, fetched, strict=False):
                    entry: dict[str, Any] = {
                        "lan": sub.get("lan") if isinstance(sub, dict) else None,
                        "lan_doc": sub.get("lan_doc") if isinstance(sub, dict) else None,
                    }
                    entry.update(fetch_result)
                    content.append(entry)
            else:
                content = []

            rows.append({
                "page_index": idx,
                "cid": cid,
                "part": part,
                "result": index_safe,
                "content": content,
            })

    return {"pages": pages, "subtitle": rows}


__all__ = [
    "_fetch_subtitle_body",
    "_missing_url_error",
    "_normalise_subtitle_url",
    "fetch_video_subtitle_item",
]
