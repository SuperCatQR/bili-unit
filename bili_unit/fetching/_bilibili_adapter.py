# _bilibili_adapter — bilibili-api-python calls for fetching.

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from bilibili_api import Credential, request_settings, select_client, user
from bilibili_api.article import Article, ArticleList
from bilibili_api.channel_series import ChannelOrder
from bilibili_api.exceptions import (
    ApiException,
    InitialStateException,
)
from bilibili_api.opus import Opus
from bilibili_api.video import Video

from . import RequestError, ResourceUnavailableError
from ._adapter_core import (
    _PERMANENT_BUSINESS_CODES as _CORE_PERMANENT_BUSINESS_CODES,
)
from ._adapter_core import (
    extract_list_items as _extract_list_items,
)
from ._adapter_core import (
    extract_total_count as _extract_total_count,
)
from ._adapter_core import (
    json_safe as _json_safe,
)
from ._adapter_core import (
    map_bilibili_errors as _map_bilibili_errors,
)
from ._adapter_core import (
    normalise_api_result as _normalise_api_result,
)
from ._adapters import _video as _video_adapter
from ._adapters._pagination import _PAGINATION_STRATEGIES as _PAGINATION_STRATEGIES
from ._adapters._subtitle import (
    _fetch_subtitle_body as _fetch_subtitle_body,
)
from ._adapters._subtitle import (
    _missing_url_error as _missing_url_error,
)
from ._adapters._subtitle import (
    _normalise_subtitle_url as _normalise_subtitle_url,
)
from ._adapters._subtitle import (
    fetch_video_subtitle_item as fetch_video_subtitle_item,
)
from ._endpoint_spec import EndpointSpec

logger = logging.getLogger("bili.fetching.adapter")

_PERMANENT_BUSINESS_CODES = _CORE_PERMANENT_BUSINESS_CODES
_extract_bvids_from_videos = _video_adapter._extract_bvids_from_videos


def _sync_video_adapter_patch_target() -> None:
    """Keep legacy ``_bilibili_adapter.Video`` patch target effective."""
    _video_adapter.Video = Video


async def _call_video_adapter(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    _sync_video_adapter_patch_target()
    fn = getattr(_video_adapter, name)
    return await fn(*args, **kwargs)


async def fetch_video_detail_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_detail_item", *args, **kwargs)


async def _video_pages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    _sync_video_adapter_patch_target()
    return await _video_adapter._video_pages(*args, **kwargs)


async def fetch_video_pages_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_pages_item", *args, **kwargs)


async def fetch_video_detail_full_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_detail_full_item", *args, **kwargs)


async def fetch_video_ai_conclusion_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_ai_conclusion_item", *args, **kwargs)


async def fetch_video_danmaku_snapshot_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_danmaku_snapshot_item", *args, **kwargs)


async def fetch_video_danmaku_view_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_danmaku_view_item", *args, **kwargs)


async def fetch_video_danmaku_xml_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_danmaku_xml_item", *args, **kwargs)


async def fetch_video_danmakus_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_danmakus_item", *args, **kwargs)


async def fetch_video_online_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_online_item", *args, **kwargs)


async def fetch_video_pay_coins_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_pay_coins_item", *args, **kwargs)


async def fetch_video_pbp_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_pbp_item", *args, **kwargs)


async def fetch_video_player_info_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_player_info_item", *args, **kwargs)


async def fetch_video_private_notes_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_private_notes_item", *args, **kwargs)


async def fetch_video_related_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_related_item", *args, **kwargs)


async def fetch_video_relation_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_relation_item", *args, **kwargs)


async def fetch_video_special_dms_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_special_dms_item", *args, **kwargs)


async def fetch_video_up_mid_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_up_mid_item", *args, **kwargs)


async def fetch_video_snapshot_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_snapshot_item", *args, **kwargs)


async def fetch_video_download_url_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_download_url_item", *args, **kwargs)


async def fetch_video_is_episode_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_is_episode_item", *args, **kwargs)


async def fetch_video_is_forbid_note_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_is_forbid_note_item", *args, **kwargs)


async def fetch_video_chargers_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call_video_adapter("fetch_video_chargers_item", *args, **kwargs)


async def _wrap_scalar_result(coro: Awaitable, key: str = "value") -> dict:
    """Wrap scalar/list API results in a dict and make them JSON-safe."""
    result = await coro
    safe = _json_safe(result)
    if isinstance(safe, dict):
        return safe
    return {key: safe}


# ---------------------------------------------------------------------------
# Item-level fan-out helpers (cf. video_detail_design.md §9.3)
# ---------------------------------------------------------------------------

async def _wrap_list_result(coro: Awaitable) -> dict:
    """Wrap an API that returns a bare list into a dict ``{"list": [...]}``.

    Some B站 APIs (e.g. get_masterpiece) return a list instead of a dict.
    ``fetch_endpoint`` requires a dict, so this adapter normalises the shape.
    """
    return _normalise_api_result(await coro, key="list")


async def fetch_user_channels(
    uid: int,
    cred: Credential | None = None,
    timeout: float = 30.0,
    **_kw: Any,
) -> dict[str, Any]:
    """Fetch all ChannelSeries objects and serialise their metadata.

    The parameter is named ``cred`` (not ``credential``) to match the call
    convention in :func:`fetch_endpoint` (``spec.callable(uid, cred=...)``)
    and the helper-generated signatures from :func:`_user_method`.  Using
    ``credential`` here would silently route the value into ``**_kw`` and
    leave the required argument unbound — every call would then raise a
    misleading ``RequestError: missing 1 required positional argument``
    that the runner mistakes for a transient failure and burns the full
    retry budget on.
    """
    u = user.User(uid, credential=cred)
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


async def fetch_user_media_list(
    uid: int,
    cred: Credential | None = None,
    timeout: float = 30.0,
    sort_field: int | user.MedialistOrder = user.MedialistOrder.PUBDATE,
    **kw: Any,
) -> dict[str, Any]:
    """Fetch one page of the user's "video sets" (media list).

    The bilibili-api SDK's ``get_media_list`` accesses ``sort_field.value`` so
    it must receive a :class:`bilibili_api.user.MedialistOrder` enum.  But the
    runner persists ``request_params`` as JSON (for resume/progress) — and
    enums aren't JSON-serialisable, which previously crashed the page-save
    silently and stranded the endpoint in RUNNING.  This wrapper accepts the
    int form (which round-trips through JSON cleanly) and re-casts it before
    invoking the SDK.

    The parameter is named ``cred`` to match the call convention in
    :func:`fetch_endpoint` (``spec.callable(uid, cred=...)``); see
    :func:`fetch_user_channels` for the rationale.
    """
    if isinstance(sort_field, int):
        try:
            sort_enum = user.MedialistOrder(sort_field)
        except ValueError as exc:
            raise RequestError(
                f"media_list: invalid sort_field {sort_field!r}: {exc}",
            ) from exc
    else:
        sort_enum = sort_field

    u = user.User(uid, credential=cred)
    async with _map_bilibili_errors("media_list"):
        return await asyncio.wait_for(
            u.get_media_list(sort_field=sort_enum, **kw),
            timeout=timeout,
        )


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


# ---------------------------------------------------------------------------


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
    strategy_fn = _PAGINATION_STRATEGIES[spec.pagination_strategy]
    is_last, next_req = strategy_fn(spec, data, request_params)

    return FetchPageResult(
        uid=uid,
        endpoint=spec.name,
        raw_payload=data,
        is_last_page=is_last,
        next_request=next_req,
    )
