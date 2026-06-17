# Auto-split from _endpoint_catalog; endpoint facts stay unchanged.

from __future__ import annotations

from bilibili_api import user

from .._bilibili_adapter import (
    _user_method,
    _wrap_scalar_result,
    fetch_user_channels,
    fetch_user_media_list,
)
from .._endpoint_spec import EndpointSpec


def user_endpoints() -> list[EndpointSpec]:
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
        EndpointSpec(
            name="access_id",
            callable=lambda uid, cred=None, **kw: (
                _wrap_scalar_result(
                    user.User(uid, credential=cred).get_access_id(),
                    key="access_id",
                )
            ),
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="access_id",
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
        EndpointSpec(
            name="dynamics_legacy",
            callable=_user_method("get_dynamics", offset=0, need_top=False),
            credential_required=False,
            params_strategy={"offset": 0, "need_top": False},
            pagination_strategy="legacy_offset",
            rate_limit_key="dynamics_legacy",
            item_id_path="cards[*].desc.dynamic_id",
            items_path="cards",
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
        EndpointSpec(
            name="channels",
            callable=fetch_user_channels,
            credential_required=False,
            pagination_strategy="none",
            rate_limit_key="channels",
        ),
        EndpointSpec(
            # ``sort_field`` is the int form of ``user.MedialistOrder`` so the
            # value is JSON-safe in ``params_strategy`` (the runner persists it
            # as progress).  ``fetch_user_media_list`` re-casts it to the enum
            # before invoking the SDK (``get_media_list`` calls ``.value`` on
            # it).  Embedding the enum directly here would crash progress
            # serialisation with ``TypeError: not JSON serializable`` and
            # silently leave the endpoint stuck in RUNNING.
            name="media_list",
            callable=fetch_user_media_list,
            credential_required=False,
            params_strategy={
                "oid": None,
                "ps": 100,
                "direction": True,
                "desc": True,
                "sort_field": int(user.MedialistOrder.PUBDATE.value),
                "tid": 0,
                "with_current": False,
            },
            pagination_strategy="oid",
            rate_limit_key="media_list",
            item_id_paths=["media_list[*].bvid", "list[*].bvid", "items[*].bvid"],
            items_path="media_list",
        ),
    ]

__all__ = ["user_endpoints"]
