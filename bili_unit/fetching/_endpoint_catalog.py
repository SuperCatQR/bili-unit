# _endpoint_catalog -- endpoint metadata registry for fetching.

from __future__ import annotations

from bilibili_api import user

from ._bilibili_adapter import (
    _extract_bvids_from_videos,
    _extract_cvids_from_articles,
    _extract_opus_ids_from_opus,
    _extract_rlids_from_article_list,
    _extract_season_ids,
    _extract_series_ids,
    _paginate_channel_videos,
    _user_method,
    _wrap_list_result,
    fetch_article_detail_item,
    fetch_article_list_detail_item,
    fetch_opus_detail_item,
    fetch_video_detail_item,
)
from ._endpoint_spec import EndpointSpec


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


ENDPOINTS: list[EndpointSpec] = _build_endpoints()


def get_endpoint(name: str) -> EndpointSpec | None:
    for ep in ENDPOINTS:
        if ep.name == name:
            return ep
    return None


