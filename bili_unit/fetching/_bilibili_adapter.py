# _bilibili_adapter — bilibili-api-python calls for fetching.

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
from bilibili_api import Credential, request_settings, select_client, user
from bilibili_api.article import Article, ArticleList
from bilibili_api.channel_series import ChannelOrder
from bilibili_api.exceptions import (
    ApiException,
    InitialStateException,
    NetworkException,
    ResponseCodeException,
)
from bilibili_api.opus import Opus
from bilibili_api.video import Video

from . import (
    Http5xxError,
    Http412Error,
    RequestError,
    ResourceUnavailableError,
)
from ._endpoint_spec import EndpointSpec

logger = logging.getLogger("bili.fetching.adapter")


# ---------------------------------------------------------------------------
# Permanent (non-retryable) B站 business codes.
#
# These codes describe a stable state of the resource — retrying yields the
# same response — so the runner should mark the endpoint / item permanently
# failed without consuming the retry budget.
#
#   53013 — 用户隐私设置未公开 (privacy: list withheld)
#   88214 — up未开通充电        (charging not enabled)
#
# Add new codes here only after confirming they are terminal: the user has
# opted out, the resource is gated behind a permission, or the feature is
# disabled.  Transient codes (rate limit / auth / server) must NOT live here.
# ---------------------------------------------------------------------------

_PERMANENT_BUSINESS_CODES: frozenset[int] = frozenset({53013, 88214})


# ---------------------------------------------------------------------------
# Common bilibili-api error → fetching error mapping
#
# Every per-call site in this module funnels through the same six-arm except
# chain: TimeoutError, ResponseCodeException(412 / permanent / other),
# NetworkException, ApiException, and a bare-Exception sweep.  The chain is
# centralised here so each call site reads as a single line.
#
# ``passthrough`` lets a call site declare exception types that should NOT be
# mapped — they bubble up so an outer try/except can apply site-specific
# logic (e.g. article_detail's InitialStateException / KeyError → permanent;
# opus_detail's ApiException sub-branch on "opus_id 不正确" / "fallback").
# Listed types are checked BEFORE the bare-Exception sweep so they are not
# silently swallowed into RequestError.
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _map_bilibili_errors(
    label: str,
    *,
    passthrough: tuple[type[BaseException], ...] = (),
):
    try:
        yield
    except TimeoutError as exc:
        raise Http5xxError(f"{label}: timeout") from exc
    except ResponseCodeException as exc:
        if exc.code == 412:
            raise Http412Error(f"{label}: 412") from exc
        if exc.code in _PERMANENT_BUSINESS_CODES:
            raise ResourceUnavailableError(
                f"{label}: code={exc.code}: {exc.msg}",
            ) from exc
        raise RequestError(f"{label}: code={exc.code}: {exc.msg}") from exc
    except NetworkException as exc:
        raise Http5xxError(f"{label}: network error {exc}") from exc
    except passthrough:
        # Site-specific exception types that the caller wants to handle in an
        # outer try/except (e.g. article_detail's InitialStateException /
        # KeyError → permanent; opus_detail's ApiException sub-branch on
        # "opus_id 不正确" / "fallback").  Placed AFTER ResponseCodeException
        # and NetworkException so those arms still take precedence — otherwise
        # passthrough=(ApiException,) would swallow 412 / permanent-code
        # handling, since ResponseCodeException is an ApiException subclass.
        raise
    except ApiException as exc:
        raise RequestError(f"{label}: {exc}") from exc
    except Exception as exc:
        raise RequestError(f"{label}: unexpected: {exc}") from exc


def _resolve_dot_path(data: dict[str, Any], path: str) -> Any:
    """Navigate a nested dict using a dot-separated path.

    Pure key-traversal (no ``[*]`` expansion).  Returns *None* when any
    segment is missing or a type mismatch occurs.
    """
    current: Any = data
    for seg in path.split("."):
        if not seg:
            continue
        if isinstance(current, dict) and seg in current:
            current = current[seg]
        else:
            return None
    return current


def _json_safe(value: Any) -> Any:
    """Convert bilibili-api return objects into JSON-serialisable values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(v) for v in value]
    if hasattr(value, "__dict__"):
        return {
            str(k): _json_safe(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


async def _wrap_scalar_result(coro: Awaitable, key: str = "value") -> dict:
    """Wrap scalar/list API results in a dict and make them JSON-safe."""
    result = await coro
    safe = _json_safe(result)
    if isinstance(safe, dict):
        return safe
    return {key: safe}


def _extract_list_items(data: dict[str, Any], path: str | None = None) -> list:
    """Best-effort extraction for common B站 paginated list shapes."""
    container: Any = _resolve_dot_path(data, path) if path else None
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in ("list", "items", "archives", "data", "media_list"):
            value = container.get(key)
            if isinstance(value, list):
                return value
        collected: list = []
        for value in container.values():
            if isinstance(value, list):
                collected.extend(value)
        return collected

    for key in ("list", "items", "archives", "data", "media_list", "cards"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("list", "items", "archives", "data", "media_list"):
                nested_value = value.get(nested)
                if isinstance(nested_value, list):
                    return nested_value
    return []


def _extract_total_count(data: dict[str, Any]) -> int:
    """Best-effort total-count extraction across known B站 shapes."""
    for path in (
        "page.count",
        "page.total",
        "items_lists.page.total",
        "total",
        "count",
        "total_count",
        "totalSize",
    ):
        value = _resolve_dot_path(data, path)
        if isinstance(value, int):
            return value
    return 0


def _normalise_api_result(result: Any, key: str = "data") -> dict[str, Any]:
    safe = _json_safe(result)
    if isinstance(safe, dict):
        return safe
    return {key: safe}


# ---------------------------------------------------------------------------
# Item-level fan-out helpers (cf. video_detail_design.md §9.3)
# ---------------------------------------------------------------------------

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


async def _wrap_list_result(coro: Awaitable) -> dict:
    """Wrap an API that returns a bare list into a dict ``{"list": [...]}``.

    Some B站 APIs (e.g. get_masterpiece) return a list instead of a dict.
    ``fetch_endpoint`` requires a dict, so this adapter normalises the shape.
    """
    return _normalise_api_result(await coro, key="list")


async def fetch_user_channels(
    uid: int,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch all ChannelSeries objects and serialise their metadata."""
    u = user.User(uid, credential=credential)
    async with _map_bilibili_errors("channels"):
        channels = await asyncio.wait_for(u.get_channels(), timeout=timeout)

    rows: list[dict[str, Any]] = []
    for ch in channels:
        try:
            ch_type = ch.get_type()
            ch_id = ch.get_id()
            async with _map_bilibili_errors(f"channels[{ch_id}]: get_meta"):
                meta = await asyncio.wait_for(ch.get_meta(), timeout=timeout)
            rows.append({
                "id": ch_id,
                "type": _json_safe(ch_type),
                "meta": _json_safe(meta),
            })
        except Exception as exc:
            raise RequestError(f"channels: serialise failed: {exc}") from exc
    return {"channels": rows}


def _extract_qa_ids_from_upower_qa(raw_payload: dict) -> list[str]:
    """Extract qa_ids from upower_qa pages."""
    ids: list[str] = []
    for page in raw_payload.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        for item in page.get("list", []) or []:
            if not isinstance(item, dict):
                continue
            qa_id = item.get("qa_id")
            if qa_id is not None:
                ids.append(str(qa_id))
    return ids


async def fetch_upower_qa_detail_item(
    qa_id: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **kw: Any,
) -> dict[str, Any]:
    """Fetch a single charging Q&A detail by qa_id."""
    try:
        qa_id_int = int(qa_id)
        uid = int(kw["_uid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestError(f"upower_qa_detail[{qa_id}]: invalid input: {exc}") from exc

    u = user.User(uid, credential=credential)
    async with _map_bilibili_errors(f"upower_qa_detail[{qa_id}]"):
        result = await asyncio.wait_for(
            u.get_upower_qa_detail(qa_id_int),
            timeout=timeout,
        )
    return _normalise_api_result(result, key="detail")


async def fetch_video_detail_item(
    bvid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch get_info + get_tags for a single bvid. Returns {"info": ..., "tags": ...}."""
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
            rows.append({
                "page_index": idx,
                "cid": cid,
                "part": page.get("part", "") if isinstance(page, dict) else "",
                "result": _json_safe(result),
            })
        return {"pages": pages, key: rows}

    return _fn


fetch_video_pages_item = _video_item_method("get_pages", result_key="pages")
fetch_video_detail_full_item = _video_item_method("get_detail", result_key="detail")
fetch_video_ai_conclusion_item = _video_item_method(
    "get_ai_conclusion", per_page=True, page_arg="both", result_key="ai_conclusion",
)
fetch_video_danmaku_snapshot_item = _video_item_method(
    "get_danmaku_snapshot", result_key="danmaku_snapshot",
)
fetch_video_danmaku_view_item = _video_item_method(
    "get_danmaku_view", per_page=True, page_arg="both", result_key="danmaku_view",
)
fetch_video_danmaku_xml_item = _video_item_method(
    "get_danmaku_xml", per_page=True, page_arg="both", result_key="danmaku_xml",
)
fetch_video_danmakus_item = _video_item_method(
    "get_danmakus", per_page=True, page_arg="both", result_key="danmakus",
)
fetch_video_online_item = _video_item_method(
    "get_online", per_page=True, page_arg="both", result_key="online",
)
fetch_video_pay_coins_item = _video_item_method("get_pay_coins", result_key="pay_coins")
fetch_video_pbp_item = _video_item_method(
    "get_pbp", per_page=True, page_arg="both", result_key="pbp",
)
fetch_video_player_info_item = _video_item_method(
    "get_player_info", per_page=True, page_arg="cid", result_key="player_info",
)
fetch_video_private_notes_item = _video_item_method(
    "get_private_notes_list", result_key="private_notes",
)
fetch_video_related_item = _video_item_method("get_related", result_key="related")
fetch_video_relation_item = _video_item_method("get_relation", result_key="relation")
fetch_video_special_dms_item = _video_item_method(
    "get_special_dms", per_page=True, page_arg="both", result_key="special_dms",
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
    "get_download_url", per_page=True, page_arg="both", result_key="download_url",
)
fetch_video_is_episode_item = _video_item_method("is_episode", result_key="is_episode")
fetch_video_is_forbid_note_item = _video_item_method(
    "is_forbid_note", result_key="is_forbid_note",
)
fetch_video_chargers_item = _video_item_method("get_chargers", result_key="chargers")


# ---------------------------------------------------------------------------
# Item-level fan-out helpers — video_subtitle (per-page subtitle index + body)
# ---------------------------------------------------------------------------
#
# B 站字幕端点的两段抓取语义 (cf. PLAN.md W1.1):
#   1. ``Video.get_subtitle(cid)`` 返回每个 page 的字幕索引；其中
#      ``subtitles[*]`` 的每条记录里有 ``subtitle_url``、``lan``、``lan_doc``
#      等元数据 —— url 通常是缺 scheme 的协议相对地址 (``//i0.hdslb.com/...``)。
#   2. 索引拿到后还要再去 CDN 拉每个 url 的 JSON 正文，里面的 ``body`` 字段
#      才是 ``[{from, to, content}, ...]`` 的真正字幕段。
#
# 失败处理：单条 lang 拉失败 (timeout / network / JSON parse) 不阻塞整个端点 ——
# 只在该 lang 项里写 ``_fetch_error`` 字段，``content`` 不带 ``body``。
# 整页无字幕时 ``content`` 是 ``[]``。

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


async def _missing_url_error() -> dict[str, Any]:
    """Awaitable stub returned when a subtitles entry has no ``subtitle_url``."""
    return {"_fetch_error": "missing subtitle_url"}


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


# ---------------------------------------------------------------------------
# Item-level fan-out helpers — article_detail (per-cvid body)
# ---------------------------------------------------------------------------


def _extract_cvids_from_articles(raw_payload: dict) -> list[str]:
    """Extract all article cvids from articles endpoint raw_payload (pages shape).

    The articles endpoint paginates with shape ``{pages: [{articles: [...]}]}``;
    each ``articles[*].id`` is the cvid (int) — we stringify for stable IDs.
    """
    cvids: list[str] = []
    for page in raw_payload.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        for art in page.get("articles", []) or []:
            if not isinstance(art, dict):
                continue
            cvid = art.get("id")
            if cvid is not None:
                cvids.append(str(cvid))
    return cvids


async def fetch_article_detail_item(
    cvid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch article body + info for a single cvid.

    Returns ``{"info": ..., "markdown": "...", "content_json": [...]}``.
    ``info`` is the article metadata (mirrors the list-level fields plus the
    bits that only get_info exposes); ``markdown`` is the rendered body;
    ``content_json`` is the structured node tree (for callers that need
    image lists / latex / cards without re-parsing markdown).

    Per docs/bili-api-info/modules/article.md: ``fetch_content`` is a
    side-effect method — it populates internal state, then ``markdown()`` /
    ``json()`` read from it.  We call them in sequence here, mapping
    bilibili-api exceptions to our retry-aware Http* / RequestError types.
    """
    try:
        cvid_int = int(cvid)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"article_detail[{cvid}]: invalid cvid: {exc}") from exc

    a = Article(cvid_int, credential=credential)

    async with _map_bilibili_errors(f"article_detail[{cvid}]: get_info"):
        info = await asyncio.wait_for(a.get_info(), timeout=timeout)

    # ``fetch_content`` scrapes the article web page for
    # ``window.__INITIAL_STATE__`` (cf. bilibili_api.utils.initial_state).
    # When the article is taken down, gated behind risk-control, or the
    # response is a placeholder shell, the marker is absent — bilibili-api
    # raises ``InitialStateException("未找到相关信息")``, or a bare
    # ``KeyError`` for ``readInfo``.  Both are terminal — surface as
    # permanent so the runner skips instead of burning the retry budget.
    # ``passthrough`` lets these escape ``_map_bilibili_errors`` unmapped
    # so the outer try/except below can rewrap them.
    try:
        async with _map_bilibili_errors(
            f"article_detail[{cvid}]: fetch_content",
            passthrough=(InitialStateException, KeyError),
        ):
            await asyncio.wait_for(a.fetch_content(), timeout=timeout)
            markdown_text: str = a.markdown()
            content_json: list[Any] = a.json()
    except InitialStateException as exc:
        raise ResourceUnavailableError(
            f"article_detail[{cvid}]: fetch_content {exc} "
            f"(article unavailable / page returns no initial state)",
        ) from exc
    except KeyError as exc:
        raise ResourceUnavailableError(
            f"article_detail[{cvid}]: fetch_content missing key {exc} "
            f"(article unavailable / page structure changed)",
        ) from exc

    return {
        "info": info,
        "markdown": markdown_text,
        "content_json": content_json,
    }


# ---------------------------------------------------------------------------
# Item-level fan-out helpers — opus_detail (per-opus_id body)
# ---------------------------------------------------------------------------


def _extract_opus_ids_from_opus(raw_payload: dict) -> list[str]:
    """Extract all opus_ids from opus endpoint raw_payload (pages shape).

    The opus endpoint paginates with shape ``{pages: [{items: [...], offset, has_more}]}``;
    each ``items[*].opus_id`` is the opus_id (string-ish) — we coerce to ``str``
    for stable IDs across pages.
    """
    ids: list[str] = []
    for page in raw_payload.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        for it in page.get("items", []) or []:
            if not isinstance(it, dict):
                continue
            oid = it.get("opus_id")
            if oid is not None:
                ids.append(str(oid))
    return ids


async def fetch_opus_detail_item(
    opus_id: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch opus body + info for a single opus_id.

    Returns ``{"info": ..., "markdown": "...", "images": [...]}``.
    ``info`` is the opus detail payload (modules, basic, stats, etc.);
    ``markdown`` is the rendered body via ``Opus.markdown()``;
    ``images`` is the raw image-info list (URL, width, height, ...) which
    callers need without re-walking ``modules.module_content.paragraphs``.

    Per docs/bili-api-info/modules/opus.md: ``markdown()`` /
    ``get_images_raw_info()`` internally call ``get_info()`` and read from
    its cached state — we still call ``get_info()`` once explicitly so that
    a missing opus surfaces as a clean failure before we touch the body.
    """
    try:
        opus_id_int = int(opus_id)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"opus_detail[{opus_id}]: invalid opus_id: {exc}") from exc

    o = Opus(opus_id_int, credential=credential)

    # ``Opus.get_info`` raises ArgsException("传入的 opus_id 不正确") when the
    # API returns a fallback marker — that is a terminal state, not a
    # retryable network failure.  ``passthrough=(ApiException,)`` lets the
    # bare ApiException escape the mapper so we can branch on the message
    # here; ResponseCodeException / NetworkException still map to
    # 412 / permanent / 5xx through their dedicated arms.
    try:
        async with _map_bilibili_errors(
            f"opus_detail[{opus_id}]: get_info",
            passthrough=(ApiException,),
        ):
            info = await asyncio.wait_for(o.get_info(), timeout=timeout)
    except ApiException as exc:
        if "opus_id 不正确" in str(exc) or "fallback" in str(exc).lower():
            raise ResourceUnavailableError(
                f"opus_detail[{opus_id}]: opus unavailable ({exc})",
            ) from exc
        raise RequestError(f"opus_detail[{opus_id}]: get_info {exc}") from exc

    # ``markdown()`` walks ``info.item.modules`` and indexes nested keys; an
    # unexpected page shape (taken-down opus, schema drift) leaks a bare
    # ``KeyError`` — surface as permanent, the same way article_detail
    # treats a missing ``readInfo``.
    try:
        async with _map_bilibili_errors(
            f"opus_detail[{opus_id}]: markdown",
            passthrough=(KeyError,),
        ):
            markdown_text: str = await asyncio.wait_for(o.markdown(), timeout=timeout)
            images: list[dict[str, Any]] = await asyncio.wait_for(
                o.get_images_raw_info(), timeout=timeout,
            )
    except KeyError as exc:
        raise ResourceUnavailableError(
            f"opus_detail[{opus_id}]: markdown missing key {exc} "
            f"(opus unavailable / shape changed)",
        ) from exc

    return {
        "info": info,
        "markdown": markdown_text,
        "images": images,
    }


# ---------------------------------------------------------------------------
# Item-level fan-out helpers — article_list_detail (per-rlid article list)
# ---------------------------------------------------------------------------


def _extract_rlids_from_article_list(raw_payload: dict) -> list[str]:
    """Extract all rlids from article_list endpoint raw_payload.

    The article_list endpoint returns ``{lists: [{id, mid, name, ...}], total}``;
    each ``lists[*].id`` is the rlid (int) — we stringify for stable IDs.
    """
    ids: list[str] = []
    for lst in raw_payload.get("lists", []) or []:
        if not isinstance(lst, dict):
            continue
        rlid = lst.get("id")
        if rlid is not None:
            ids.append(str(rlid))
    return ids


async def fetch_article_list_detail_item(
    rlid: str,
    credential: Credential | None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch the cvid roster of a single readlist (文集).

    Returns the raw API payload from
    ``https://api.bilibili.com/x/article/list/web/articles?id=<rlid>`` —
    a dict with ``list`` (the readlist meta), ``articles`` (the cvid roster,
    each entry includes ``id`` / ``title`` / ``stats`` / ``publish_time``),
    and ``author``.

    Per docs/bili-api-info/modules/article.md ``ArticleList.get_content()``:
    a single API call, no scraping involved (unlike article_detail), so the
    common bilibili-api error families suffice — no InitialState / KeyError
    fallbacks needed.
    """
    try:
        rlid_int = int(rlid)
    except (TypeError, ValueError) as exc:
        raise RequestError(
            f"article_list_detail[{rlid}]: invalid rlid: {exc}",
        ) from exc

    al = ArticleList(rlid_int, credential=credential)

    async with _map_bilibili_errors(
        f"article_list_detail[{rlid}]: get_content",
    ):
        result = await asyncio.wait_for(al.get_content(), timeout=timeout)

    return result


# ---------------------------------------------------------------------------
# Item-level fan-out helpers — channel_videos_season / channel_videos_series
# ---------------------------------------------------------------------------

def _extract_season_ids(raw_payload: dict) -> list[str]:
    """Extract season IDs from channel_list endpoint raw_payload."""
    ids: list[str] = []
    for page in raw_payload.get("pages", []):
        items_lists = page.get("items_lists", {})
        for item in items_lists.get("seasons_list", []):
            meta = item.get("meta", {})
            sid = meta.get("season_id")
            if sid is not None:
                ids.append(str(sid))
    return ids


def _extract_series_ids(raw_payload: dict) -> list[str]:
    """Extract series IDs from channel_list endpoint raw_payload."""
    ids: list[str] = []
    for page in raw_payload.get("pages", []):
        items_lists = page.get("items_lists", {})
        for item in items_lists.get("series_list", []):
            meta = item.get("meta", {})
            sid = meta.get("series_id")
            if sid is not None:
                ids.append(str(sid))
    return ids


async def _paginate_channel_videos(
    kind: str,
    uid: int,
    sid: int,
    credential: Credential | None,
    timeout: float = 30.0,
    ps: int = 100,
    **_kw: Any,
) -> dict[str, Any]:
    """Internal helper: paginate through all videos in a season or series.

    ``kind`` is ``"season"`` or ``"series"``.
    Returns ``{"archives": [...], "page": {"count": N}}`` with all pages merged.
    """
    u = user.User(uid, credential=credential)
    all_archives: list[Any] = []
    pn = 1
    while True:
        async with _map_bilibili_errors(f"channel_videos_{kind}[{sid}]"):
            if kind == "season":
                data = await asyncio.wait_for(
                    u.get_channel_videos_season(
                        sid=sid, sort=ChannelOrder.DEFAULT, pn=pn, ps=ps,
                    ),
                    timeout=timeout,
                )
            else:
                data = await asyncio.wait_for(
                    u.get_channel_videos_series(
                        sid=sid, sort=ChannelOrder.DEFAULT, pn=pn, ps=ps,
                    ),
                    timeout=timeout,
                )

        archives = data.get("archives", [])
        all_archives.extend(archives)

        page_info = data.get("page", {})
        total = page_info.get("count", 0)
        if not archives or (total > 0 and pn * ps >= total):
            break
        pn += 1

    return {"archives": all_archives, "page": {"count": len(all_archives)}}


def _user_method(name: str, **defaults: Any):
    """Build a uid-level callable that dispatches to ``User.{name}``.

    Most uid-level endpoints follow the same shape:
    ``user.User(uid, credential=cred).{method}(**kw_merged_with_defaults)``.
    This helper removes ~3 lines of boilerplate per endpoint.
    """
    def _fn(uid, cred=None, **kw):
        merged = {**defaults, **kw}
        return getattr(user.User(uid, credential=cred), name)(**merged)
    return _fn


# ---------------------------------------------------------------------------
# HTTP backend bootstrap
# ---------------------------------------------------------------------------

def init_http_backend(backend: str = "aiohttp", impersonate: str = "chrome131") -> None:
    """Called once at startup to configure bilibili-api-python's HTTP backend."""
    try:
        import curl_cffi  # noqa: F401

        if backend == "curl_cffi":
            select_client("curl_cffi")
            request_settings.set("impersonate", impersonate)
            logger.info("HTTP backend: curl_cffi (impersonate=%s)", impersonate)
            return
    except ImportError:
        if backend == "curl_cffi":
            logger.warning(
                "curl_cffi not installed; falling back to aiohttp"
            )

    # default / fallback
    select_client("aiohttp")
    logger.info("HTTP backend: aiohttp")


# ---------------------------------------------------------------------------
# Fetch a single page
# ---------------------------------------------------------------------------

@dataclass
class FetchPageResult:
    uid: int
    endpoint: str
    raw_payload: dict[str, Any]
    is_last_page: bool = False
    next_request: dict[str, Any] | None = None


async def fetch_endpoint(
    uid: int,
    spec: EndpointSpec,
    credential: Credential | None,
    request_params: dict[str, Any],
    timeout: float = 30.0,
) -> FetchPageResult:
    """Call one page of an endpoint and return the raw payload."""
    async with _map_bilibili_errors(spec.name):
        data = await asyncio.wait_for(
            spec.callable(uid, cred=credential, **request_params),
            timeout=timeout,
        )

    # determine pagination
    is_last = False
    next_req: dict[str, Any] | None = None

    if spec.pagination_strategy == "none":
        is_last = True
        next_req = None
    elif spec.pagination_strategy == "page":
        # Generic page pagination: detect list items and total count.
        # Supports multiple B站 response shapes:
        #   videos:       {"list": {"vlist": [...]}, "page": {"count": N}}
        #   audios:       {"data": [...], "curPage": 1, "pageCount": N, "totalSize": N}
        #   channel_list: {"items_lists": {"page": {"total": N}, "seasons_list": [...], ...}}
        #
        # --- pagination info ---
        total_count = _extract_total_count(data)

        # Shape 1: standard B站 {"page": {"count": N}} (videos)
        pi = data.get("page")
        if isinstance(pi, dict):
            total_count = total_count or pi.get("count", 0)

        # Shape 2: audio service top-level fields
        if total_count == 0 and "totalSize" in data:
            total_count = data.get("totalSize", 0)

        # Shape 3: channel_list {"items_lists": {"page": {"total": N}}}
        if total_count == 0:
            il_page = _resolve_dot_path(data, "items_lists.page")
            if isinstance(il_page, dict):
                total_count = il_page.get("total", 0)

        # Shape 4: articles {"articles": [...], "pn": N, "ps": N, "count": N}
        if total_count == 0 and "count" in data and isinstance(data["count"], int):
            total_count = data["count"]

        # Shape 5: album {"biz_list": [...], "total_count": N}
        if total_count == 0 and "total_count" in data and isinstance(data["total_count"], int):
            total_count = data["total_count"]

        # --- items ---
        items = _extract_list_items(data, spec.items_path)

        current_pn = request_params.get("pn", 1)
        ps = request_params.get("ps", 30)
        if not items or (total_count > 0 and current_pn * ps >= total_count):
            is_last = True
        else:
            next_req = {**request_params, "pn": current_pn + 1}

    elif spec.pagination_strategy == "cursor":
        has_more = data.get("has_more", 0) == 1
        if not has_more:
            is_last = True
        else:
            next_req = {**request_params, "offset": data.get("offset", "")}

    elif spec.pagination_strategy == "anchor":
        # Anchor pagination: response contains an ``anchor`` field pointing to
        # the next page's start.  Terminate when anchor is absent or 0.
        anchor = data.get("anchor", 0)
        if not anchor:
            is_last = True
        else:
            next_req = {**request_params, "anchor": anchor}

    elif spec.pagination_strategy == "legacy_offset":
        next_offset = data.get("next_offset", 0)
        has_more = data.get("has_more", 0) == 1
        if not has_more or not next_offset:
            is_last = True
        else:
            next_req = {**request_params, "offset": next_offset}

    elif spec.pagination_strategy == "oid":
        items = _extract_list_items(data, spec.items_path)
        ps = request_params.get("ps", 100)
        total_count = _extract_total_count(data)
        if not items or (total_count > 0 and len(items) >= total_count):
            is_last = True
        else:
            last = items[-1] if isinstance(items[-1], dict) else {}
            next_oid = (
                last.get("aid")
                or last.get("id")
                or last.get("oid")
                or last.get("param")
            )
            if not next_oid or len(items) < ps:
                is_last = True
            else:
                next_req = {**request_params, "oid": next_oid}

    return FetchPageResult(
        uid=uid,
        endpoint=spec.name,
        raw_payload=data,
        is_last_page=is_last,
        next_request=next_req,
    )
