# tests for bili_unit/fetching/client
# Run: uv run pytest bili_unit/tests/test_client.py -v

from unittest.mock import patch

import pytest

from bili_unit.fetching import Http412Error
from bili_unit.fetching._bilibili_adapter import fetch_endpoint
from bili_unit.fetching._endpoint_catalog import ENDPOINTS, get_endpoint


def test_endpoints_exist():
    names = [ep.name for ep in ENDPOINTS]
    assert "user_info" in names
    assert "videos" in names
    assert "relation_info" in names
    assert "up_stat" in names


def test_get_endpoint():
    ep = get_endpoint("user_info")
    assert ep is not None
    assert ep.name == "user_info"
    assert ep.pagination_strategy == "none"

    ep = get_endpoint("videos")
    assert ep is not None
    assert ep.pagination_strategy == "page"

    assert get_endpoint("nonexistent") is None


def test_new_endpoints_exist():
    ri = get_endpoint("relation_info")
    assert ri is not None
    assert ri.pagination_strategy == "none"
    us = get_endpoint("up_stat")
    assert us is not None
    assert us.pagination_strategy == "none"


# --- T1 endpoint registration tests ---


def test_t1_overview_stat_registered():
    ep = get_endpoint("overview_stat")
    assert ep is not None
    assert ep.pagination_strategy == "none"
    assert ep.kind == "uid"
    assert ep.rate_limit_key == "overview_stat"


@pytest.mark.asyncio
async def test_fetch_endpoint_overview_stat():
    spec = get_endpoint("overview_stat")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"archive": {"view": 10000}, "article": {"view": 5000}, "likes": 800}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(123, spec, None, {})
    assert result.is_last_page
    assert result.next_request is None
    assert result.raw_payload["archive"]["view"] == 10000


# --- T1: articles (page pagination) ---


def test_t1_articles_registered():
    ep = get_endpoint("articles")
    assert ep is not None
    assert ep.pagination_strategy == "page"
    assert ep.kind == "uid"
    assert ep.rate_limit_key == "articles"
    assert ep.item_id_path == "articles[*].id"
    assert ep.items_path == "articles"


@pytest.mark.asyncio
async def test_fetch_endpoint_articles_page():
    spec = get_endpoint("articles")
    assert spec is not None

    async def fake_page(uid, cred=None, **kw):
        pn = kw.get("pn", 1)
        if pn <= 1:
            return {"articles": [{"id": i} for i in range(30)], "page": {"count": 45}}
        return {"articles": [{"id": i} for i in range(30, 45)], "page": {"count": 45}}

    with patch.object(spec, "callable", fake_page):
        r1 = await fetch_endpoint(123, spec, None, {"pn": 1, "ps": 30})
        assert not r1.is_last_page
        assert r1.next_request == {"pn": 2, "ps": 30}

        r2 = await fetch_endpoint(123, spec, None, r1.next_request)
        assert r2.is_last_page


@pytest.mark.asyncio
async def test_fetch_endpoint_articles_top_level_count():
    """Articles API returns count at top level, not under page.count."""
    spec = get_endpoint("articles")
    assert spec is not None

    async def fake_page(uid, cred=None, **kw):
        pn = kw.get("pn", 1)
        if pn == 1:
            return {"articles": [{"id": i} for i in range(30)], "pn": 1, "ps": 30, "count": 50}
        return {"articles": [{"id": i} for i in range(30, 50)], "pn": 2, "ps": 30, "count": 50}

    with patch.object(spec, "callable", fake_page):
        r1 = await fetch_endpoint(123, spec, None, {"pn": 1, "ps": 30})
        assert not r1.is_last_page
        assert r1.next_request == {"pn": 2, "ps": 30}

        r2 = await fetch_endpoint(123, spec, None, r1.next_request)
        assert r2.is_last_page


# --- T1: subscribed_bangumi (page pagination) ---


def test_t1_subscribed_bangumi_registered():
    ep = get_endpoint("subscribed_bangumi")
    assert ep is not None
    assert ep.pagination_strategy == "page"
    assert ep.kind == "uid"
    assert ep.rate_limit_key == "subscribed_bangumi"
    assert ep.item_id_path == "list[*].season_id"
    assert ep.items_path == "list"


@pytest.mark.asyncio
async def test_fetch_endpoint_subscribed_bangumi_page():
    spec = get_endpoint("subscribed_bangumi")
    assert spec is not None

    async def fake_page(uid, cred=None, **kw):
        pn = kw.get("pn", 1)
        if pn <= 1:
            return {"list": [{"season_id": i} for i in range(15)], "page": {"count": 25}}
        return {"list": [{"season_id": i} for i in range(15, 25)], "page": {"count": 25}}

    with patch.object(spec, "callable", fake_page):
        r1 = await fetch_endpoint(123, spec, None, {"pn": 1, "ps": 15})
        assert not r1.is_last_page
        assert r1.next_request == {"pn": 2, "ps": 15}

        r2 = await fetch_endpoint(123, spec, None, r1.next_request)
        assert r2.is_last_page


# --- T1: opus (cursor pagination) ---


def test_t1_opus_registered():
    ep = get_endpoint("opus")
    assert ep is not None
    assert ep.pagination_strategy == "cursor"
    assert ep.kind == "uid"
    assert ep.rate_limit_key == "opus"
    assert ep.item_id_path == "items[*].opus_id"
    assert ep.items_path == "items"


@pytest.mark.asyncio
async def test_fetch_endpoint_opus_cursor():
    spec = get_endpoint("opus")
    assert spec is not None

    async def fake_cursor(uid, cred=None, **kw):
        offset = kw.get("offset", "")
        if offset == "":
            return {
                "items": [{"opus_id": str(i)} for i in range(10)],
                "has_more": 1,
                "offset": "10",
            }
        return {
            "items": [{"opus_id": str(i)} for i in range(10, 15)],
            "has_more": 0,
            "offset": "15",
        }

    with patch.object(spec, "callable", fake_cursor):
        r1 = await fetch_endpoint(123, spec, None, {"offset": ""})
        assert not r1.is_last_page
        assert r1.next_request == {"offset": "10"}

        r2 = await fetch_endpoint(123, spec, None, r1.next_request)
        assert r2.is_last_page
        assert r2.next_request is None


@pytest.mark.asyncio
async def test_fetch_endpoint_relation_info():
    spec = get_endpoint("relation_info")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"follower": 100, "following": 50}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(123, spec, None, {})
    assert result.is_last_page
    assert result.raw_payload["follower"] == 100


@pytest.mark.asyncio
async def test_fetch_endpoint_up_stat():
    spec = get_endpoint("up_stat")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"archive": {"view": 10000}, "article": {"view": 5000}}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(123, spec, None, {})
    assert result.is_last_page
    assert "archive" in result.raw_payload


@pytest.mark.asyncio
async def test_fetch_endpoint_success():
    """Mock bilibili_api.user.User to return fake data."""
    spec = get_endpoint("user_info")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"code": 0, "data": {"mid": uid, "name": "test"}}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(123, spec, None, {})
    assert result.uid == 123
    assert result.is_last_page
    assert result.raw_payload["data"]["name"] == "test"


@pytest.mark.asyncio
async def test_fetch_endpoint_412():
    from bilibili_api.exceptions import ResponseCodeException

    spec = get_endpoint("videos")
    assert spec is not None

    async def fake_412(uid, cred=None, **kw):
        raise ResponseCodeException(412, "too fast", {})

    with patch.object(spec, "callable", fake_412), pytest.raises(Http412Error):
        await fetch_endpoint(123, spec, None, {"pn": 1, "ps": 30})


@pytest.mark.asyncio
async def test_fetch_endpoint_pagination_page():
    spec = get_endpoint("videos")
    assert spec is not None

    async def fake_page(uid, cred=None, **kw):
        pn = kw.get("pn", 1)
        if pn <= 2:
            return {"list": {"vlist": [{"aid": pn * 100 + i} for i in range(30)]}, "page": {"count": 65}}
        return {"list": {"vlist": [{"aid": 999}]}, "page": {"count": 65}}

    with patch.object(spec, "callable", fake_page):
        r1 = await fetch_endpoint(123, spec, None, {"pn": 1, "ps": 30})
        assert not r1.is_last_page
        assert r1.next_request == {"pn": 2, "ps": 30}

        r2 = await fetch_endpoint(123, spec, None, r1.next_request)
        assert not r2.is_last_page

        r3 = await fetch_endpoint(123, spec, None, r2.next_request)
        assert r3.is_last_page


# ======================================================================
# T2 — endpoint registration
# ======================================================================

_T2_UID_NONE = [
    "user_medal", "space_notice", "all_followings", "top_videos",
    "masterpiece", "article_list", "cheese", "elec_monthly",
]

_T2_CRED_REQUIRED = {"user_medal", "all_followings", "elec_monthly", "upower_qa"}


def test_t2_uid_none_registered():
    """All T2 uid-level none-paginated endpoints are registered."""
    for name in _T2_UID_NONE:
        ep = get_endpoint(name)
        assert ep is not None, f"{name} not registered"
        assert ep.pagination_strategy == "none", f"{name} should be 'none'"
        assert ep.kind == "uid", f"{name} should be kind='uid'"


def test_t2_credential_required():
    """Credential-required T2 endpoints are marked correctly."""
    for name in _T2_CRED_REQUIRED:
        ep = get_endpoint(name)
        assert ep is not None, f"{name} not registered"
        assert ep.credential_required is True, f"{name} should require credential"


def test_t2_user_fav_tag_registered():
    ep = get_endpoint("user_fav_tag")
    assert ep is not None
    assert ep.pagination_strategy == "page"
    assert ep.params_strategy == {"pn": 1, "ps": 20}


def test_t2_album_registered():
    ep = get_endpoint("album")
    assert ep is not None
    assert ep.pagination_strategy == "page"
    assert ep.items_path == "biz_list"
    assert ep.params_strategy == {"pn": 1, "ps": 30}


def test_t2_channel_videos_season_registered():
    ep = get_endpoint("channel_videos_season")
    assert ep is not None
    assert ep.kind == "item"
    assert ep.source_endpoint == "channel_list"
    assert ep.pagination_strategy == "none"
    assert ep.extract_items is not None


def test_t2_channel_videos_series_registered():
    ep = get_endpoint("channel_videos_series")
    assert ep is not None
    assert ep.kind == "item"
    assert ep.source_endpoint == "channel_list"
    assert ep.pagination_strategy == "none"
    assert ep.extract_items is not None


def test_t2_upower_qa_registered():
    ep = get_endpoint("upower_qa")
    assert ep is not None
    assert ep.pagination_strategy == "anchor"
    assert ep.params_strategy == {"anchor": 0}
    assert ep.credential_required is True


# ======================================================================
# T2 — helper function tests
# ======================================================================


def test_wrap_list_result_with_list():
    import asyncio

    from bili_unit.fetching._bilibili_adapter import _wrap_list_result

    async def fake():
        return [{"a": 1}, {"a": 2}]

    result = asyncio.get_event_loop().run_until_complete(_wrap_list_result(fake()))
    assert result == {"list": [{"a": 1}, {"a": 2}]}


def test_wrap_list_result_with_dict():
    import asyncio

    from bili_unit.fetching._bilibili_adapter import _wrap_list_result

    async def fake():
        return {"key": "value"}

    result = asyncio.get_event_loop().run_until_complete(_wrap_list_result(fake()))
    assert result == {"key": "value"}


def test_extract_season_ids():
    from bili_unit.fetching._bilibili_adapter import _extract_season_ids

    payload = {
        "pages": [
            {
                "items_lists": {
                    "seasons_list": [
                        {"meta": {"season_id": 123, "name": "Season A"}},
                        {"meta": {"season_id": 456, "name": "Season B"}},
                    ],
                    "series_list": [],
                },
            },
        ],
    }
    assert _extract_season_ids(payload) == ["123", "456"]


def test_extract_series_ids():
    from bili_unit.fetching._bilibili_adapter import _extract_series_ids

    payload = {
        "pages": [
            {
                "items_lists": {
                    "seasons_list": [],
                    "series_list": [
                        {"meta": {"series_id": 789, "name": "Series X"}},
                    ],
                },
            },
        ],
    }
    assert _extract_series_ids(payload) == ["789"]


def test_extract_season_ids_empty():
    from bili_unit.fetching._bilibili_adapter import _extract_season_ids

    assert _extract_season_ids({"pages": []}) == []
    assert _extract_season_ids({}) == []


# ======================================================================
# T2 — anchor pagination strategy
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_endpoint_anchor_first_page():
    """Anchor pagination: first page returns next anchor."""
    spec = get_endpoint("upower_qa")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {
            "list": [{"qa_id": 1}, {"qa_id": 2}],
            "anchor": 100,
        }

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(1, spec, None, {"anchor": 0})

    assert not result.is_last_page
    assert result.next_request == {"anchor": 100}


@pytest.mark.asyncio
async def test_fetch_endpoint_anchor_last_page():
    """Anchor pagination: anchor=0 or absent means last page."""
    spec = get_endpoint("upower_qa")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"list": [{"qa_id": 3}], "anchor": 0}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(1, spec, None, {"anchor": 100})

    assert result.is_last_page
    assert result.next_request is None


@pytest.mark.asyncio
async def test_fetch_endpoint_anchor_absent():
    """Anchor pagination: missing anchor field means last page."""
    spec = get_endpoint("upower_qa")
    assert spec is not None

    async def fake_call(uid, cred=None, **kw):
        return {"list": [{"qa_id": 4}]}

    with patch.object(spec, "callable", fake_call):
        result = await fetch_endpoint(1, spec, None, {"anchor": 50})

    assert result.is_last_page


# ======================================================================
# T2 — album page_num/page_size mapping
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_endpoint_album_page_mapping():
    """Album uses page_num/page_size internally but spec uses pn/ps."""
    spec = get_endpoint("album")
    assert spec is not None

    captured_params = {}

    async def fake_call(uid, cred=None, **kw):
        captured_params.update(kw)
        return {
            "biz_list": [{"id": 1}, {"id": 2}],
            "total_count": 5,
        }

    with patch.object(spec, "callable", fake_call):
        r1 = await fetch_endpoint(1, spec, None, {"pn": 1, "ps": 2})

    # callable receives pn/ps in kw (the lambda maps to page_num/page_size)
    assert captured_params.get("pn") == 1
    assert not r1.is_last_page
    assert r1.next_request == {"pn": 2, "ps": 2}
