# Tests for the W2.1 video_subtitle parsing model and its materializer
# handler. Covers the language-priority selection rule, the
# all-page-failed page-skip rule, ``is_complete`` semantics, dict round
# trip, and the materializer write path.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key
from bili_unit.parsing.materializer import ParsingMaterializer
from bili_unit.parsing.models.video_subtitle import (
    PARSER,
    SubtitlePage,
    SubtitleSegment,
    VideoSubtitle,
)

# ---------------------------------------------------------------------------
# from_raw — language priority + body selection
# ---------------------------------------------------------------------------

def _make_lang_entry(lan: str, body: list[dict] | None) -> dict:
    """Build a content entry — pass ``body=None`` for a fetch-error entry."""
    if body is None:
        return {"lan": lan, "lan_doc": f"doc-{lan}", "_fetch_error": "boom"}
    return {"lan": lan, "lan_doc": f"doc-{lan}", "body": body}


def _seg(start: float, end: float, text: str) -> dict:
    return {"from": start, "to": end, "content": text}


def test_from_raw_picks_zh_cn_when_present():
    raw = {
        "pages": [{"cid": 1, "part": "p1"}],
        "subtitle": [
            {
                "page_index": 0, "cid": 1, "part": "p1",
                "result": {},
                "content": [
                    _make_lang_entry("zh-CN", [_seg(0.0, 1.5, "你好"), _seg(1.5, 3.0, "世界")]),
                    _make_lang_entry("en-US", [_seg(0.0, 1.5, "hi")]),
                ],
            }
        ],
    }
    obj = VideoSubtitle.from_raw("BV001", raw)
    assert obj.bvid == "BV001"
    assert len(obj.pages) == 1
    page = obj.pages[0]
    assert page.lan == "zh-CN"
    assert page.lan_doc == "doc-zh-CN"
    assert [s.content for s in page.segments] == ["你好", "世界"]
    assert obj.is_complete is True
    # available_languages reflects every body-bearing lang (dedup, in order).
    assert set(obj.available_languages) == {"zh-CN", "en-US"}
    # source / cross refs
    assert any(ref.endpoint == "video_subtitle" and ref.item_id == "BV001"
               for ref in obj.source_refs)
    assert obj.cross_refs.bvid == "BV001"


def test_from_raw_lang_priority_ai_zh_beats_en():
    raw = {
        "pages": [{"cid": 2, "part": "p1"}],
        "subtitle": [
            {
                "page_index": 0, "cid": 2, "part": "p1",
                "result": {},
                "content": [
                    _make_lang_entry("ai-zh", [_seg(0.0, 1.0, "AI 中")]),
                    _make_lang_entry("en", [_seg(0.0, 1.0, "english")]),
                ],
            }
        ],
    }
    obj = VideoSubtitle.from_raw("BV002", raw)
    assert obj.pages[0].lan == "ai-zh"
    assert [s.content for s in obj.pages[0].segments] == ["AI 中"]


def test_from_raw_zh_hk_beats_ai_zh():
    raw = {
        "pages": [{}],
        "subtitle": [
            {"page_index": 0, "cid": 3, "content": [
                _make_lang_entry("ai-zh", [_seg(0.0, 1.0, "ai")]),
                _make_lang_entry("zh-HK", [_seg(0.0, 1.0, "粵")]),
            ]},
        ],
    }
    obj = VideoSubtitle.from_raw("BV003", raw)
    assert obj.pages[0].lan == "zh-HK"


def test_from_raw_falls_back_to_first_when_no_priority_match():
    raw = {
        "subtitle": [
            {"page_index": 0, "cid": 4, "content": [
                _make_lang_entry("ja", [_seg(0.0, 1.0, "こん")]),
                _make_lang_entry("ko", [_seg(0.0, 1.0, "안")]),
            ]},
        ],
    }
    obj = VideoSubtitle.from_raw("BV004", raw)
    # First usable wins when no priority hits.
    assert obj.pages[0].lan == "ja"


def test_from_raw_skips_page_with_only_fetch_errors():
    raw = {
        "subtitle": [
            {"page_index": 0, "cid": 10, "content": [
                _make_lang_entry("zh-CN", [_seg(0.0, 1.0, "ok")]),
            ]},
            {"page_index": 1, "cid": 11, "content": [
                _make_lang_entry("zh-CN", None),
                _make_lang_entry("en", None),
            ]},
        ],
    }
    obj = VideoSubtitle.from_raw("BV005", raw)
    # Only the page with usable body survives.
    assert len(obj.pages) == 1
    assert obj.pages[0].page_index == 0
    # ``is_complete`` reflects the gap (page 1 lost).
    assert obj.is_complete is True  # bool over surviving pages
    # But the second page never gets a SubtitlePage — caller must judge
    # completeness against the fetched page count externally.


def test_is_complete_true_when_all_pages_resolved():
    raw = {
        "subtitle": [
            {"page_index": 0, "cid": 1, "content": [
                _make_lang_entry("zh-CN", [_seg(0, 1, "a")]),
            ]},
            {"page_index": 1, "cid": 2, "content": [
                _make_lang_entry("zh-CN", [_seg(0, 1, "b")]),
            ]},
        ],
    }
    obj = VideoSubtitle.from_raw("BV006", raw)
    assert len(obj.pages) == 2
    assert obj.is_complete is True


def test_is_complete_false_when_no_pages():
    raw = {"subtitle": []}
    obj = VideoSubtitle.from_raw("BV007", raw)
    assert obj.pages == []
    assert obj.is_complete is False


def test_is_complete_false_when_a_page_has_no_lan():
    """A SubtitlePage with empty ``lan`` (constructed by hand) trips
    ``is_complete``; ``from_raw`` skips such pages so this exercises the
    ``from_dict`` round-trip path."""
    obj = VideoSubtitle(
        bvid="BV008",
        pages=[
            SubtitlePage(page_index=0, cid=1, lan="zh-CN",
                          segments=[SubtitleSegment(0, 1, "ok")]),
            SubtitlePage(page_index=1, cid=2, lan="",
                          segments=[]),
        ],
    )
    assert obj.is_complete is False


def test_round_trip_dict():
    raw = {
        "subtitle": [
            {"page_index": 0, "cid": 100, "content": [
                _make_lang_entry("zh-CN", [_seg(0.0, 1.5, "你好"), _seg(1.5, 3.0, "世界")]),
                _make_lang_entry("en-US", [_seg(0.0, 1.5, "hi")]),
            ]},
            {"page_index": 1, "cid": 101, "content": [
                _make_lang_entry("ai-zh", [_seg(0.0, 2.0, "AI")]),
            ]},
        ],
    }
    obj = VideoSubtitle.from_raw("BV009", raw)
    d = obj.to_dict()
    # Schema metadata
    assert d["_model_name"] == "video_subtitle"
    assert d["_schema_version"] == 1
    assert d["bvid"] == "BV009"
    assert d["is_complete"] is True
    # Round trip through from_dict
    revived = VideoSubtitle.from_dict(d)
    assert revived.bvid == obj.bvid
    assert revived.is_complete == obj.is_complete
    assert len(revived.pages) == len(obj.pages)
    for p1, p2 in zip(revived.pages, obj.pages, strict=True):
        assert p1.page_index == p2.page_index
        assert p1.cid == p2.cid
        assert p1.lan == p2.lan
        assert [s.content for s in p1.segments] == [s.content for s in p2.segments]
    assert revived.cross_refs.bvid == "BV009"


def test_parser_alias():
    assert PARSER is VideoSubtitle


# ---------------------------------------------------------------------------
# Materializer — _parse_video_subtitle
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def parsing_store(tmp_path):
    store = ParsingDataStore(tmp_path / "parsing")
    await store.open()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_materializer_writes_video_subtitle(parsing_store):
    """``_parse_video_subtitle`` reads fetching fanout payloads and writes
    one VideoSubtitle row per bvid."""
    raw_a = {
        "pages": [{"cid": 1, "part": "p1"}],
        "subtitle": [
            {"page_index": 0, "cid": 1, "content": [
                {"lan": "zh-CN", "lan_doc": "中文",
                 "body": [{"from": 0.0, "to": 1.0, "content": "嗨"}]},
            ]},
        ],
    }
    raw_b = {
        "subtitle": [
            {"page_index": 0, "cid": 2, "content": [
                {"lan": "en-US", "lan_doc": "English",
                 "body": [{"from": 0.0, "to": 1.5, "content": "hi"}]},
            ]},
        ],
    }
    fake_fetch = MagicMock()
    fake_fetch.list_fanout_payloads = AsyncMock(return_value={
        "BVA1": raw_a, "BVB2": raw_b,
    })
    materializer = ParsingMaterializer(parsing_store, fake_fetch)

    count = await materializer.parse_model(uid=42, model_name="video_subtitle", mode="full")

    assert count == 2
    fake_fetch.list_fanout_payloads.assert_awaited_with(42, "video_subtitle")

    obj_a = await parsing_store.get(_item_key(42, "video_subtitle", "BVA1"))
    assert obj_a is not None
    assert obj_a["bvid"] == "BVA1"
    assert obj_a["pages"][0]["lan"] == "zh-CN"

    obj_b = await parsing_store.get(_item_key(42, "video_subtitle", "BVB2"))
    assert obj_b is not None
    assert obj_b["pages"][0]["lan"] == "en-US"


@pytest.mark.asyncio
async def test_materializer_returns_zero_when_no_fanout(parsing_store):
    """No fanout entries → handler returns 0 without writing anything."""
    fake_fetch = MagicMock()
    fake_fetch.list_fanout_payloads = AsyncMock(return_value={})
    materializer = ParsingMaterializer(parsing_store, fake_fetch)

    count = await materializer.parse_model(uid=43, model_name="video_subtitle", mode="full")

    assert count == 0


@pytest.mark.asyncio
async def test_materializer_skips_existing_in_incremental_mode(parsing_store):
    """Existing rows are kept; only fresh bvids are written in
    ``incremental`` mode."""
    raw = {
        "subtitle": [
            {"page_index": 0, "cid": 1, "content": [
                {"lan": "zh-CN", "body": [{"from": 0.0, "to": 1.0, "content": "x"}]},
            ]},
        ],
    }
    fake_fetch = MagicMock()
    fake_fetch.list_fanout_payloads = AsyncMock(return_value={
        "BV01": raw, "BV02": raw,
    })
    materializer = ParsingMaterializer(parsing_store, fake_fetch)

    # Pre-seed BV01 so it should be skipped.
    await parsing_store.put(
        _item_key(99, "video_subtitle", "BV01"),
        {"_model_name": "video_subtitle", "bvid": "BV01"},
    )

    count = await materializer.parse_model(
        uid=99, model_name="video_subtitle", mode="incremental",
    )

    assert count == 1  # only BV02
    pre_existing = await parsing_store.get(_item_key(99, "video_subtitle", "BV01"))
    # Untouched: stored value still carries the sentinel; only ``updated_at``
    # is injected by the store on write.
    assert pre_existing is not None
    assert pre_existing.get("_model_name") == "video_subtitle"
    assert pre_existing.get("bvid") == "BV01"
    # Sanity: the materializer would have populated ``pages`` if it re-wrote
    # the row, so its absence proves the skip path.
    assert "pages" not in pre_existing
