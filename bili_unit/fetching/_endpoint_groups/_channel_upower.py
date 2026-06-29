# Auto-split from _endpoint_catalog; endpoint facts stay unchanged.

from __future__ import annotations

from .._bilibili_adapter import (
    _extract_qa_ids_from_upower_qa,
    _extract_season_ids,
    _extract_series_ids,
    _new_user,
    _paginate_channel_videos,
    _user_method,
    _wrap_list_result,
    fetch_upower_qa_detail_item,
)
from .._endpoint_spec import EndpointSpec

# Enum default values baked in as raw SDK values so this module imports without
# touching ``bilibili_api`` (F2 IPC §8: main process zero-import).  Mirrors:
#   user.OrderType.desc          -> "desc"
#   user.ArticleListOrder.LATEST -> 0
#   user.AlbumType.ALL           -> "all"
_ORDER_TYPE_DESC = "desc"
_ARTICLE_LIST_ORDER_LATEST = 0
_ALBUM_TYPE_ALL = "all"


def channel_and_upower_endpoints() -> list[EndpointSpec]:
    return [
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
            name="live_info",
            callable=_user_method("get_live_info"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="live_info",
        ),
        EndpointSpec(
            name="user_relation",
            callable=_user_method("get_relation"),
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="user_relation",
        ),
        EndpointSpec(
            name="reservation",
            callable=_user_method("get_reservation"),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="reservation",
        ),
        EndpointSpec(
            name="uplikeimg",
            callable=_user_method("get_uplikeimg"),
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="uplikeimg",
        ),
        EndpointSpec(
            name="top_followers",
            callable=_user_method("top_followers", since=None),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="top_followers",
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
            name="followings",
            callable=_user_method("get_followings", pn=1, ps=100, attention=False, order=_ORDER_TYPE_DESC),
            credential_required=True,
            params_strategy={"pn": 1, "ps": 100},
            pagination_strategy="page",
            rate_limit_key="followings",
            item_id_path="list[*].mid",
            items_path="list",
        ),
        EndpointSpec(
            name="followers",
            callable=_user_method("get_followers", pn=1, ps=100, desc=True),
            credential_required=True,
            params_strategy={"pn": 1, "ps": 100},
            pagination_strategy="page",
            rate_limit_key="followers",
            item_id_path="list[*].mid",
            items_path="list",
        ),
        EndpointSpec(
            name="same_followers",
            callable=_user_method("get_self_same_followers", pn=1, ps=50),
            credential_required=True,
            params_strategy={"pn": 1, "ps": 50},
            pagination_strategy="page",
            rate_limit_key="same_followers",
            item_id_path="list[*].mid",
            items_path="list",
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
                    _new_user(uid, credential=cred).get_masterpiece()
                )
            ),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="masterpiece",
        ),
        EndpointSpec(
            name="article_list",
            callable=_user_method("get_article_list", order=_ARTICLE_LIST_ORDER_LATEST),
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
                _new_user(uid, credential=cred).get_album(
                    biz=kw.get("biz", _ALBUM_TYPE_ALL),
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
        EndpointSpec(
            name="upower_qa_detail",
            callable=fetch_upower_qa_detail_item,
            credential_required=True,
            pagination_strategy="none",
            rate_limit_key="upower_qa_detail",
            kind="item",
            source_endpoint="upower_qa",
            extract_items=_extract_qa_ids_from_upower_qa,
            needs_parent_uid=True,
        ),
    ]

__all__ = ["channel_and_upower_endpoints"]
