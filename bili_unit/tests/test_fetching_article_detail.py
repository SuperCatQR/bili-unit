# tests for article_detail (item-level fan-out endpoint).
# Mirrors test_fetching_video_detail.py — same shape, different module.

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import (
    Http412Error,
    RequestError,
    ResourceUnavailableError,
)
from bili_unit.fetching.client import (
    _extract_cvids_from_articles,
    fetch_article_detail_item,
    get_endpoint,
)

# ======================================================================
# Client — _extract_cvids_from_articles
# ======================================================================

def test_extract_cvids_from_articles_basic():
    payload = {
        "pages": [
            {"articles": [{"id": 100}, {"id": 200}]},
        ],
    }
    assert _extract_cvids_from_articles(payload) == ["100", "200"]


def test_extract_cvids_from_articles_multi_page():
    payload = {
        "pages": [
            {"articles": [{"id": 1}, {"id": 2}]},
            {"articles": [{"id": 3}]},
        ],
    }
    assert _extract_cvids_from_articles(payload) == ["1", "2", "3"]


def test_extract_cvids_from_articles_empty():
    assert _extract_cvids_from_articles({"pages": []}) == []
    assert _extract_cvids_from_articles({}) == []


def test_extract_cvids_from_articles_skips_missing_id():
    payload = {
        "pages": [
            {"articles": [{"id": 1}, {"title": "no-id"}]},
        ],
    }
    assert _extract_cvids_from_articles(payload) == ["1"]


def test_extract_cvids_from_articles_tolerates_malformed_pages():
    payload = {
        "pages": [
            None,
            "garbage",
            {"articles": [{"id": 7}, "garbage"]},
        ],
    }
    assert _extract_cvids_from_articles(payload) == ["7"]


# ======================================================================
# Client — article_detail endpoint registration
# ======================================================================

def test_article_detail_endpoint_registered():
    spec = get_endpoint("article_detail")
    assert spec is not None
    assert spec.name == "article_detail"
    assert spec.kind == "item"
    assert spec.source_endpoint == "articles"
    assert spec.rate_limit_key == "article_detail"
    assert spec.pagination_strategy == "none"
    assert spec.extract_items is not None


# ======================================================================
# Client — fetch_article_detail_item
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_article_detail_item_success():
    fake_info = {"id": 42, "title": "hello", "summary": "s"}
    fake_md = "# Title\n\nbody text"
    fake_json = [{"type": "ParagraphNode", "text": "body text"}]

    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(return_value=fake_info)
        instance.fetch_content = AsyncMock(return_value=None)
        instance.markdown = lambda: fake_md
        instance.json = lambda: fake_json

        result = await fetch_article_detail_item("42", None)

    assert result == {
        "info": fake_info,
        "markdown": fake_md,
        "content_json": fake_json,
    }
    # Article(cvid_int) — must be parsed as int.
    MockArticle.assert_called_once()
    args, kwargs = MockArticle.call_args
    assert args[0] == 42
    assert kwargs.get("credential") is None


@pytest.mark.asyncio
async def test_fetch_article_detail_item_invalid_cvid_raises_request_error():
    with pytest.raises(RequestError, match="invalid cvid"):
        await fetch_article_detail_item("not-an-int", None)


@pytest.mark.asyncio
async def test_fetch_article_detail_item_info_412_maps_to_http412():
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(412, "too fast", {}),
        )

        with pytest.raises(Http412Error):
            await fetch_article_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_article_detail_item_fetch_content_error_propagates():
    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(return_value={"id": 1})
        instance.fetch_content = AsyncMock(side_effect=RequestError("body fetch"))

        with pytest.raises(RequestError, match="body fetch"):
            await fetch_article_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_article_detail_item_keyerror_maps_to_unavailable():
    """KeyError from fetch_content (missing readInfo, taken-down articles)
    must map to :class:`ResourceUnavailableError` so the runner skips retries.
    """
    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(return_value={"id": 1})
        instance.fetch_content = AsyncMock(side_effect=KeyError("readInfo"))

        with pytest.raises(ResourceUnavailableError, match="readInfo"):
            await fetch_article_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_article_detail_item_permanent_business_code_maps_to_unavailable():
    """Permanent business codes (e.g. 53013) must surface as ResourceUnavailableError."""
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(53013, "用户隐私设置未公开", {}),
        )

        with pytest.raises(ResourceUnavailableError, match="53013"):
            await fetch_article_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_article_detail_item_initial_state_maps_to_unavailable():
    """``InitialStateException`` from ``fetch_content`` (taken-down articles whose
    page no longer embeds ``window.__INITIAL_STATE__``) must surface as
    :class:`ResourceUnavailableError` so the runner skips retries instead of
    burning the budget on the same shell page.
    """
    from bilibili_api.exceptions import InitialStateException

    with patch("bili_unit.fetching.client.Article") as MockArticle:
        instance = MockArticle.return_value
        instance.get_info = AsyncMock(return_value={"id": 1})
        instance.fetch_content = AsyncMock(
            side_effect=InitialStateException("未找到相关信息"),
        )

        with pytest.raises(ResourceUnavailableError, match="未找到相关信息"):
            await fetch_article_detail_item("1", None)
