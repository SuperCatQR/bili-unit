# tests for article_list_detail (item-level fan-out endpoint).
# Mirrors test_fetching_article_detail.py — same shape, different module.

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import (
    Http412Error,
    RequestError,
    ResourceUnavailableError,
)
from bili_unit.fetching.client import (
    _extract_rlids_from_article_list,
    fetch_article_list_detail_item,
    get_endpoint,
)

# ======================================================================
# Client — _extract_rlids_from_article_list
# ======================================================================

def test_extract_rlids_basic():
    payload = {
        "lists": [
            {"id": 1043430, "name": "【警戒追踪】"},
            {"id": 1043431, "name": "【前哨速递】"},
        ],
        "total": 2,
    }
    assert _extract_rlids_from_article_list(payload) == ["1043430", "1043431"]


def test_extract_rlids_empty():
    assert _extract_rlids_from_article_list({"lists": []}) == []
    assert _extract_rlids_from_article_list({}) == []


def test_extract_rlids_skips_missing_id():
    payload = {
        "lists": [
            {"id": 1},
            {"name": "no-id"},
            {"id": 2},
        ],
    }
    assert _extract_rlids_from_article_list(payload) == ["1", "2"]


def test_extract_rlids_tolerates_malformed_entries():
    payload = {
        "lists": [
            None,
            "garbage",
            {"id": 7},
        ],
    }
    assert _extract_rlids_from_article_list(payload) == ["7"]


def test_extract_rlids_stringifies_int_ids():
    payload = {"lists": [{"id": 1043430}]}
    out = _extract_rlids_from_article_list(payload)
    assert out == ["1043430"]
    assert all(isinstance(x, str) for x in out)


# ======================================================================
# Client — article_list_detail endpoint registration
# ======================================================================

def test_article_list_detail_endpoint_registered():
    spec = get_endpoint("article_list_detail")
    assert spec is not None
    assert spec.name == "article_list_detail"
    assert spec.kind == "item"
    assert spec.source_endpoint == "article_list"
    assert spec.rate_limit_key == "article_list_detail"
    assert spec.pagination_strategy == "none"
    assert spec.extract_items is not None


# ======================================================================
# Client — fetch_article_list_detail_item
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_article_list_detail_item_success():
    fake_resp = {
        "list": {"id": 1043430, "name": "【警戒追踪】", "articles_count": 6},
        "articles": [
            {"id": 47634040, "title": "x"},
            {"id": 47744686, "title": "y"},
        ],
        "author": {"mid": 3546785614137774},
    }

    with patch("bili_unit.fetching.client.ArticleList") as MockArticleList:
        instance = MockArticleList.return_value
        instance.get_content = AsyncMock(return_value=fake_resp)

        result = await fetch_article_list_detail_item("1043430", None)

    assert result == fake_resp
    MockArticleList.assert_called_once_with(1043430, credential=None)


@pytest.mark.asyncio
async def test_fetch_article_list_detail_item_invalid_rlid_raises_request_error():
    with pytest.raises(RequestError, match="invalid rlid"):
        await fetch_article_list_detail_item("not-a-number", None)


@pytest.mark.asyncio
async def test_fetch_article_list_detail_item_412_maps_to_http412():
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.ArticleList") as MockArticleList:
        instance = MockArticleList.return_value
        instance.get_content = AsyncMock(
            side_effect=ResponseCodeException(412, "412", {}),
        )

        with pytest.raises(Http412Error):
            await fetch_article_list_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_article_list_detail_item_permanent_business_code_maps_to_unavailable():
    """Permanent business codes (e.g. 53013) must surface as ResourceUnavailableError."""
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.ArticleList") as MockArticleList:
        instance = MockArticleList.return_value
        instance.get_content = AsyncMock(
            side_effect=ResponseCodeException(53013, "用户隐私设置未公开", {}),
        )

        with pytest.raises(ResourceUnavailableError, match="53013"):
            await fetch_article_list_detail_item("1", None)
