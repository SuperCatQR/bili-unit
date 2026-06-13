# client — fetching scripts; strictly calls bilibili-api-python per bili-api-info.

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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

logger = logging.getLogger("bili.fetching.client")


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


# ---------------------------------------------------------------------------
# Endpoint registry (cf. fetching_engineering.md §10)
# ---------------------------------------------------------------------------

PaginationStrategy = str  # "none" | "page" | "cursor" | "anchor" | "oid" | "custom"


@dataclass
class EndpointSpec:
    name: str
    callable: Callable[..., Awaitable[dict]]
    credential_required: bool = False
    params_strategy: dict[str, Any] = field(default_factory=dict)
    pagination_strategy: PaginationStrategy = "none"
    rate_limit_key: str = ""
    item_id_path: str | None = None   # dot-path with [*] for incremental ID extraction
    item_id_paths: list[str] | None = None  # multiple paths (overrides item_id_path when set)
    items_path: str | None = None     # dot-path (no [*]) locating the items list for pagination
    kind: str = "uid"                 # "uid" | "item"
    source_endpoint: str | None = None  # required when kind="item"
    extract_items: Callable[[dict], list[str]] | None = None  # required when kind="item"
    needs_parent_uid: bool = False  # True when callable needs parent uid via kw["_uid"]


# Endpoint registry (MVP + initial extensions)
ENDPOINTS: list[EndpointSpec] = []


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
    result = await coro
    if isinstance(result, list):
        return {"list": result}
    if isinstance(result, dict):
        return result
    return {"data": result}


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


def _build_endpoints() -> list[EndpointSpec]:
    return [
        # --- MVP ---
        EndpointSpec(
            name="user_info",
            callable=_user_method("get_user_info"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="user_info",
        ),
        EndpointSpec(
            name="videos",
            callable=_user_method("get_videos", pn=1, ps=30, tid=0, keyword="", order=user.VideoOrder.PUBDATE),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 30},
            pagination_strategy="page",
            rate_limit_key="videos",
            item_id_path="list.vlist[*].bvid",
            items_path="list.vlist",
        ),
        # --- extension: relation + stat ---
        EndpointSpec(
            name="relation_info",
            callable=_user_method("get_relation_info"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="relation_info",
        ),
        EndpointSpec(
            name="up_stat",
            callable=_user_method("get_up_stat"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="up_stat",
        ),
        # --- T1: overview_stat ---
        EndpointSpec(
            name="overview_stat",
            callable=_user_method("get_overview_stat"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="overview_stat",
        ),
        # --- T1: articles ---
        EndpointSpec(
            name="articles",
            callable=_user_method("get_articles", pn=1, ps=30, order=user.ArticleOrder.PUBDATE),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 30},
            pagination_strategy="page",
            rate_limit_key="articles",
            item_id_path="articles[*].id",
            items_path="articles",
        ),
        # --- T1: subscribed_bangumi ---
        EndpointSpec(
            name="subscribed_bangumi",
            callable=_user_method("get_subscribed_bangumi", pn=1, ps=15, type_=user.BangumiType.BANGUMI, follow_status=user.BangumiFollowStatus.ALL),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 15},
            pagination_strategy="page",
            rate_limit_key="subscribed_bangumi",
            item_id_path="list[*].season_id",
            items_path="list",
        ),
        # --- T1: opus ---
        EndpointSpec(
            name="opus",
            callable=_user_method("get_opus", type_=user.OpusType.ALL, offset=""),
            credential_required=False,
            params_strategy={"offset": ""},
            pagination_strategy="cursor",
            rate_limit_key="opus",
            item_id_path="items[*].opus_id",
            items_path="items",
        ),
        # --- extension: dynamics ---
        EndpointSpec(
            name="dynamics",
            callable=_user_method("get_dynamics_new", offset=""),
            credential_required=False,
            params_strategy={"offset": ""},
            pagination_strategy="cursor",
            rate_limit_key="dynamics",
            item_id_path="items[*].id_str",
            items_path="items",
        ),
        # --- extension: audios ---
        EndpointSpec(
            name="audios",
            callable=_user_method("get_audios", pn=1, ps=30, order=user.AudioOrder.PUBDATE),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 30},
            pagination_strategy="page",
            rate_limit_key="audios",
            item_id_path="data[*].id",
            items_path="data",
        ),
        # --- extension: channel_list ---
        EndpointSpec(
            name="channel_list",
            callable=_user_method("get_channel_list", pn=1, ps=20),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 20},
            pagination_strategy="page",
            rate_limit_key="channel_list",
            item_id_paths=[
                "items_lists.seasons_list[*].meta.season_id",
                "items_lists.series_list[*].meta.series_id",
            ],
            items_path="items_lists",
        ),
        # --- item-level fan-out: video_detail ---
        EndpointSpec(
            name="video_detail",
            callable=fetch_video_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="video_detail",
            kind="item",
            source_endpoint="videos",
            extract_items=_extract_bvids_from_videos,
        ),
        # --- item-level fan-out: article_detail (专栏正文) ---
        EndpointSpec(
            name="article_detail",
            callable=fetch_article_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="article_detail",
            kind="item",
            source_endpoint="articles",
            extract_items=_extract_cvids_from_articles,
        ),
        # --- item-level fan-out: opus_detail (图文正文 + 图片清单) ---
        EndpointSpec(
            name="opus_detail",
            callable=fetch_opus_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="opus_detail",
            kind="item",
            source_endpoint="opus",
            extract_items=_extract_opus_ids_from_opus,
        ),
        # --- item-level fan-out: article_list_detail (文集 → 文章 cvid 清单) ---
        EndpointSpec(
            name="article_list_detail",
            callable=fetch_article_list_detail_item,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="article_list_detail",
            kind="item",
            source_endpoint="article_list",
            extract_items=_extract_rlids_from_article_list,
        ),
        # ================================================================
        # T2 — uid-level, none pagination
        # ================================================================
        EndpointSpec(
            name="user_medal",
            callable=_user_method("get_user_medal"),
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="user_medal",
        ),
        EndpointSpec(
            name="space_notice",
            callable=_user_method("get_space_notice"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="space_notice",
        ),
        EndpointSpec(
            name="all_followings",
            callable=_user_method("get_all_followings"),
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="all_followings",
        ),
        EndpointSpec(
            name="top_videos",
            callable=_user_method("get_top_videos"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="top_videos",
        ),
        EndpointSpec(
            name="masterpiece",
            callable=lambda uid, cred=None, **kw: (
                _wrap_list_result(
                    user.User(uid, credential=cred).get_masterpiece()
                )
            ),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="masterpiece",
        ),
        EndpointSpec(
            name="article_list",
            callable=_user_method("get_article_list", order=user.ArticleListOrder.LATEST),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="article_list",
        ),
        EndpointSpec(
            name="cheese",
            callable=_user_method("get_cheese"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="cheese",
        ),
        EndpointSpec(
            name="elec_monthly",
            callable=_user_method("get_elec_user_monthly"),
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="elec_monthly",
        ),
        # ================================================================
        # T2 — uid-level, page pagination
        # ================================================================
        # NOTE: bilibili-api-python has a bug — pn/ps are commented out in
        # get_user_fav_tag, so pagination never advances at the API level.
        # We still register it as "page" for shape-detection completeness.
        EndpointSpec(
            name="user_fav_tag",
            callable=_user_method("get_user_fav_tag", pn=1, ps=20),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 20},
            pagination_strategy="page",
            rate_limit_key="user_fav_tag",
        ),
        # album uses page_num/page_size (mapped from pn/ps in the callable)
        EndpointSpec(
            name="album",
            callable=lambda uid, cred=None, **kw: (
                user.User(uid, credential=cred).get_album(
                    biz=kw.get("biz", user.AlbumType.ALL),
                    page_num=kw.get("pn", 1),
                    page_size=kw.get("ps", 30),
                )
            ),
            credential_required=False,
            params_strategy={"pn": 1, "ps": 30},
            pagination_strategy="page",
            rate_limit_key="album",
            items_path="biz_list",
        ),
        # ================================================================
        # T2 — item-level fan-out: channel_videos
        # ================================================================
        EndpointSpec(
            name="channel_videos_season",
            callable=lambda sid, cred=None, **kw: (
                _paginate_channel_videos("season", kw["_uid"], int(sid), cred)
            ),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="channel_videos_season",
            kind="item",
            source_endpoint="channel_list",
            extract_items=_extract_season_ids,
            needs_parent_uid=True,
        ),
        EndpointSpec(
            name="channel_videos_series",
            callable=lambda sid, cred=None, **kw: (
                _paginate_channel_videos("series", kw["_uid"], int(sid), cred)
            ),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="channel_videos_series",
            kind="item",
            source_endpoint="channel_list",
            extract_items=_extract_series_ids,
            needs_parent_uid=True,
        ),
        # ================================================================
        # T2 — anchor pagination
        # ================================================================
        EndpointSpec(
            name="upower_qa",
            callable=_user_method("get_upower_qa_list", anchor=0),
            credential_required=True,
            params_strategy={"anchor": 0},
            pagination_strategy="anchor",
            rate_limit_key="upower_qa",
            item_id_path="list[*].qa_id",
            items_path="list",
        ),
    ]


ENDPOINTS[:] = _build_endpoints()


def get_endpoint(name: str) -> EndpointSpec | None:
    for ep in ENDPOINTS:
        if ep.name == name:
            return ep
    return None


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
        page_info: dict[str, Any] = {}
        total_count = 0

        # Shape 1: standard B站 {"page": {"count": N}} (videos)
        pi = data.get("page")
        if isinstance(pi, dict):
            page_info = pi
            total_count = page_info.get("count", 0)

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
        items: list = []
        container: Any = None

        # Use items_path to locate the container holding list data
        if spec.items_path:
            container = _resolve_dot_path(data, spec.items_path)

        if isinstance(container, list):
            # Container is directly a list (audios: data → [...])
            items = container
        elif isinstance(container, dict):
            # Container is a dict; extract items from known keys
            items = container.get("vlist", []) or container.get("list", [])
            if not items:
                # Collect items from all list-valued keys
                # (channel_list: seasons_list + series_list)
                collected: list = []
                for v in container.values():
                    if isinstance(v, list):
                        collected.extend(v)
                items = collected
        else:
            # Fallback: locate items without items_path (legacy heuristics)
            list_data = data.get("list", {})
            if isinstance(list_data, list):
                items = list_data
            elif isinstance(list_data, dict):
                items = list_data.get("vlist", []) or list_data.get("list", [])
                if not items:
                    for v in list_data.values():
                        if isinstance(v, list):
                            items = v
                            break

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

    return FetchPageResult(
        uid=uid,
        endpoint=spec.name,
        raw_payload=data,
        is_last_page=is_last,
        next_request=next_req,
    )
