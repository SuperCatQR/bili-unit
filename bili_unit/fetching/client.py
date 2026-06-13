# client -- compatibility facade for fetching endpoint calls.
#
# Endpoint metadata lives in _endpoint_catalog; concrete bilibili-api-python
# calls and page fetching live in _bilibili_adapter. This module keeps the
# historical import path stable for runner code and tests.

from ._bilibili_adapter import (
    FetchPageResult,
    _extract_bvids_from_videos,
    _extract_cvids_from_articles,
    _extract_opus_ids_from_opus,
    _extract_rlids_from_article_list,
    _extract_season_ids,
    _extract_series_ids,
    _map_bilibili_errors,
    _paginate_channel_videos,
    _resolve_dot_path,
    _user_method,
    _wrap_list_result,
    fetch_article_detail_item,
    fetch_article_list_detail_item,
    fetch_endpoint,
    fetch_opus_detail_item,
    fetch_video_detail_item,
    init_http_backend,
)
from ._endpoint_catalog import ENDPOINTS, get_endpoint
from ._endpoint_spec import EndpointSpec, PaginationStrategy

__all__ = [
    "ENDPOINTS",
    "EndpointSpec",
    "FetchPageResult",
    "PaginationStrategy",
    "_extract_bvids_from_videos",
    "_extract_cvids_from_articles",
    "_extract_opus_ids_from_opus",
    "_extract_rlids_from_article_list",
    "_extract_season_ids",
    "_extract_series_ids",
    "_map_bilibili_errors",
    "_paginate_channel_videos",
    "_resolve_dot_path",
    "_user_method",
    "_wrap_list_result",
    "fetch_article_detail_item",
    "fetch_article_list_detail_item",
    "fetch_endpoint",
    "fetch_opus_detail_item",
    "fetch_video_detail_item",
    "get_endpoint",
    "init_http_backend",
]
