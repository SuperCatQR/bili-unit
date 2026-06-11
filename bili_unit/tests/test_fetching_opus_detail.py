# tests for opus_detail (item-level fan-out endpoint).
# Mirrors test_fetching_article_detail.py — same shape, different module.

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit.fetching import (
    Http412Error,
    RequestError,
    ResourceUnavailableError,
)
from bili_unit.fetching.client import (
    _extract_opus_ids_from_opus,
    fetch_opus_detail_item,
    get_endpoint,
)

# ======================================================================
# Client — _extract_opus_ids_from_opus
# ======================================================================

def test_extract_opus_ids_basic():
    payload = {
        "pages": [
            {"items": [{"opus_id": "100"}, {"opus_id": "200"}]},
        ],
    }
    assert _extract_opus_ids_from_opus(payload) == ["100", "200"]


def test_extract_opus_ids_multi_page():
    payload = {
        "pages": [
            {"items": [{"opus_id": 1}, {"opus_id": 2}]},
            {"items": [{"opus_id": 3}]},
        ],
    }
    # ``opus_id`` may be int or str — extractor coerces to str.
    assert _extract_opus_ids_from_opus(payload) == ["1", "2", "3"]


def test_extract_opus_ids_empty():
    assert _extract_opus_ids_from_opus({"pages": []}) == []
    assert _extract_opus_ids_from_opus({}) == []


def test_extract_opus_ids_skips_missing_id():
    payload = {
        "pages": [
            {"items": [{"opus_id": 1}, {"title": "no-id"}]},
        ],
    }
    assert _extract_opus_ids_from_opus(payload) == ["1"]


def test_extract_opus_ids_tolerates_malformed_pages():
    payload = {
        "pages": [
            None,
            "garbage",
            {"items": [{"opus_id": 7}, "garbage"]},
        ],
    }
    assert _extract_opus_ids_from_opus(payload) == ["7"]


# ======================================================================
# Client — opus_detail endpoint registration
# ======================================================================

def test_opus_detail_endpoint_registered():
    spec = get_endpoint("opus_detail")
    assert spec is not None
    assert spec.name == "opus_detail"
    assert spec.kind == "item"
    assert spec.source_endpoint == "opus"
    assert spec.rate_limit_key == "opus_detail"
    assert spec.pagination_strategy == "none"
    assert spec.extract_items is not None


# ======================================================================
# Client — fetch_opus_detail_item
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_success():
    fake_info = {"item": {"basic": {"comment_type": 11}, "modules": []}}
    fake_md = "# Title\n\nbody text"
    fake_images = [{"url": "https://i0.hdslb.com/x.jpg", "width": 100, "height": 80}]

    with patch("bili_unit.fetching.client.Opus") as MockOpus:
        instance = MockOpus.return_value
        instance.get_info = AsyncMock(return_value=fake_info)
        instance.markdown = AsyncMock(return_value=fake_md)
        instance.get_images_raw_info = AsyncMock(return_value=fake_images)

        result = await fetch_opus_detail_item("42", None)

    assert result == {
        "info": fake_info,
        "markdown": fake_md,
        "images": fake_images,
    }
    # Opus(opus_id_int) — must be parsed as int.
    MockOpus.assert_called_once()
    args, kwargs = MockOpus.call_args
    assert args[0] == 42
    assert kwargs.get("credential") is None


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_invalid_id_raises_request_error():
    with pytest.raises(RequestError, match="invalid opus_id"):
        await fetch_opus_detail_item("not-an-int", None)


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_info_412_maps_to_http412():
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Opus") as MockOpus:
        instance = MockOpus.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(412, "too fast", {}),
        )

        with pytest.raises(Http412Error):
            await fetch_opus_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_markdown_keyerror_maps_to_unavailable():
    """A bare KeyError out of ``markdown()`` (taken-down opus, schema drift)
    must surface as :class:`ResourceUnavailableError` so the runner skips retries.
    """
    with patch("bili_unit.fetching.client.Opus") as MockOpus:
        instance = MockOpus.return_value
        instance.get_info = AsyncMock(return_value={"item": {"modules": []}})
        instance.markdown = AsyncMock(side_effect=KeyError("module_content"))

        with pytest.raises(ResourceUnavailableError, match="module_content"):
            await fetch_opus_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_fallback_args_exception_maps_to_unavailable():
    """``Opus.get_info`` raises ArgsException("传入的 opus_id 不正确") for opus that
    have been taken down — runner should treat as permanent.
    """
    from bilibili_api.exceptions import ArgsException

    with patch("bili_unit.fetching.client.Opus") as MockOpus:
        instance = MockOpus.return_value
        instance.get_info = AsyncMock(
            side_effect=ArgsException("传入的 opus_id 不正确"),
        )

        with pytest.raises(ResourceUnavailableError, match="opus unavailable"):
            await fetch_opus_detail_item("1", None)


@pytest.mark.asyncio
async def test_fetch_opus_detail_item_permanent_business_code_maps_to_unavailable():
    """Permanent business codes (e.g. 53013) must surface as ResourceUnavailableError."""
    from bilibili_api.exceptions import ResponseCodeException

    with patch("bili_unit.fetching.client.Opus") as MockOpus:
        instance = MockOpus.return_value
        instance.get_info = AsyncMock(
            side_effect=ResponseCodeException(53013, "用户隐私设置未公开", {}),
        )

        with pytest.raises(ResourceUnavailableError, match="53013"):
            await fetch_opus_detail_item("1", None)
