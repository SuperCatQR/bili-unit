# tests for bili_unit.fetching._bilibili_adapter.fetch_video_subtitle_item.
#
# The subtitle fetcher does two things per page:
#   1. Calls Video.get_subtitle(cid) for the index (lan / lan_doc / subtitle_url).
#   2. GETs each subtitle_url over aiohttp and stuffs the parsed body into a
#      sibling ``content`` list.
#
# These tests cover the four W1.1 acceptance shapes:
#   - happy-path single-lang fetch
#   - per-lang URL fetch failure (other langs unaffected)
#   - empty subtitle index → content = []
#   - URL missing scheme → "https:" prefix added before GET

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from bili_unit.fetching._bilibili_adapter import fetch_video_subtitle_item


def _make_aiohttp_response(*, status: int = 200, payload: dict | None = None):
    """Return a MagicMock that quacks like an aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _patch_aiohttp_get(url_to_payload: dict[str, dict | Exception]):
    """Patch ``aiohttp.ClientSession`` so ``session.get(url)`` returns the
    payload (or raises the exception) keyed by ``url``."""

    captured_urls: list[str] = []

    def fake_get(url, *_a, **_kw):
        captured_urls.append(url)
        outcome = url_to_payload.get(url)
        if isinstance(outcome, Exception):
            # aiohttp.ClientSession.get raises immediately on a synchronous
            # error path (e.g. invalid URL) — but in real usage the error
            # surfaces inside ``async with`` / ``await resp.json()``.  Wire
            # the mock so the exception fires from ``__aenter__``.
            resp = MagicMock()
            resp.__aenter__ = AsyncMock(side_effect=outcome)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp
        return _make_aiohttp_response(payload=outcome)

    session_mock = MagicMock()
    session_mock.get = MagicMock(side_effect=fake_get)
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)

    return (
        patch(
            "bili_unit.fetching._bilibili_adapter.aiohttp.ClientSession",
            return_value=session_mock,
        ),
        captured_urls,
    )


# ---------------------------------------------------------------------------
# 1. Happy path — single page, single lang, body fetched and merged.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_fetch_single_lang_success():
    body = [
        {"from": 0.0, "to": 3.5, "content": "你好"},
        {"from": 3.5, "to": 6.0, "content": "世界"},
    ]
    url = "https://i0.hdslb.com/bfs/subtitle/zh.json"

    patcher, urls = _patch_aiohttp_get({url: {"body": body}})

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[{"cid": 111, "part": "p1"}])
        instance.get_subtitle = AsyncMock(return_value={
            "subtitles": [
                {"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": url},
            ],
        })
        with patcher:
            result = await fetch_video_subtitle_item("BV1", None)

    assert urls == [url]
    page = result["subtitle"][0]
    assert page["page_index"] == 0
    assert page["cid"] == 111
    assert page["part"] == "p1"
    # raw index preserved verbatim under "result"
    assert page["result"]["subtitles"][0]["subtitle_url"] == url
    # body merged into "content"
    assert len(page["content"]) == 1
    entry = page["content"][0]
    assert entry["lan"] == "zh-CN"
    assert entry["lan_doc"] == "中文（中国）"
    assert entry["body"] == body
    assert "_fetch_error" not in entry


# ---------------------------------------------------------------------------
# 2. One lang's URL fetch fails — other lang's body still arrives.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_fetch_one_lang_failure_does_not_block_others():
    good_url = "https://i0.hdslb.com/bfs/subtitle/zh.json"
    bad_url = "https://i0.hdslb.com/bfs/subtitle/en.json"
    good_body = [{"from": 0.0, "to": 1.0, "content": "ok"}]

    patcher, _urls = _patch_aiohttp_get({
        good_url: {"body": good_body},
        bad_url: aiohttp.ClientError("connection reset"),
    })

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[{"cid": 111, "part": "p1"}])
        instance.get_subtitle = AsyncMock(return_value={
            "subtitles": [
                {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": good_url},
                {"lan": "en-US", "lan_doc": "English", "subtitle_url": bad_url},
            ],
        })
        with patcher:
            result = await fetch_video_subtitle_item("BV1", None)

    content = result["subtitle"][0]["content"]
    assert len(content) == 2

    zh = next(c for c in content if c["lan"] == "zh-CN")
    en = next(c for c in content if c["lan"] == "en-US")

    assert zh["body"] == good_body
    assert "_fetch_error" not in zh

    assert "body" not in en
    assert "connection reset" in en["_fetch_error"]


# ---------------------------------------------------------------------------
# 3. Page has no subtitles → content is an empty list, no aiohttp activity.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_fetch_empty_index_yields_empty_content():
    patcher, urls = _patch_aiohttp_get({})

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[{"cid": 111, "part": "p1"}])
        instance.get_subtitle = AsyncMock(return_value={"subtitles": []})
        with patcher:
            result = await fetch_video_subtitle_item("BV1", None)

    page = result["subtitle"][0]
    assert page["content"] == []
    assert page["result"] == {"subtitles": []}
    assert urls == []


# ---------------------------------------------------------------------------
# 4. URL missing scheme (B 站 returns //host/...) — "https:" prefix added.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subtitle_fetch_normalises_scheme_relative_url():
    body = [{"from": 0.0, "to": 1.0, "content": "x"}]
    expected_url = "https://i0.hdslb.com/x.json"

    patcher, urls = _patch_aiohttp_get({expected_url: {"body": body}})

    with patch("bili_unit.fetching._bilibili_adapter.Video") as MockVideo:
        instance = MockVideo.return_value
        instance.get_pages = AsyncMock(return_value=[{"cid": 111, "part": "p1"}])
        instance.get_subtitle = AsyncMock(return_value={
            "subtitles": [
                {"lan": "zh-CN", "lan_doc": "中文",
                 "subtitle_url": "//i0.hdslb.com/x.json"},
            ],
        })
        with patcher:
            result = await fetch_video_subtitle_item("BV1", None)

    # The fetcher must have called GET with the scheme-prefixed URL.
    assert urls == [expected_url]
    assert result["subtitle"][0]["content"][0]["body"] == body
