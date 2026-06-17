# Lightweight smoke test that every JSON fixture under
# ``bili_unit/tests/fixtures`` parses cleanly and carries the schema
# anchors downstream tests rely on. Catches accidental fixture
# corruption (rebase artifacts, manual edits) before it cascades into
# half-skipped suites.

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# -- MiMo ASR responses -----------------------------------------------------

@pytest.mark.parametrize("name", [
    "mimo_asr_response.json",
    "mimo_asr_response_zh.json",
])
def test_mimo_asr_response_shape(name):
    payload = _load(name)
    assert "choices" in payload
    assert isinstance(payload["choices"], list) and payload["choices"]
    msg = payload["choices"][0]["message"]
    assert isinstance(msg.get("content"), str) and msg["content"]
    usage = payload["usage"]
    assert "seconds" in usage
    assert "audio_tokens" in usage["prompt_tokens_details"]


def test_mimo_asr_response_zh_is_chinese():
    payload = _load("mimo_asr_response_zh.json")
    text = payload["choices"][0]["message"]["content"]
    # Sample line is hand-authored Chinese; assert at least one CJK char
    # so a future "fix" that drops the i18n content trips the test.
    assert any("一" <= ch <= "鿿" for ch in text)
    assert payload["usage"]["seconds"] == 30
    assert payload["usage"]["prompt_tokens_details"]["audio_tokens"] == 195


@pytest.mark.parametrize("name,expected_code", [
    ("mimo_asr_token_overflow.json", "context_length_exceeded"),
    ("mimo_asr_unauthorized.json", "invalid_api_key"),
])
def test_mimo_asr_error_shape(name, expected_code):
    payload = _load(name)
    err = payload["error"]
    assert err["code"] == expected_code
    assert err["type"] == "invalid_request_error"
    assert isinstance(err["message"], str) and err["message"]


# -- parsing model goldens --------------------------------------------------

def test_parsed_article_post_fixture():
    p = _load("parsed_article_post.json")
    assert p["_model_name"] == "article_post"
    assert p["cvid"] == "100"
    assert p["markdown"]
    assert p["stats"]["view"] > 0
    assert p["image_urls"]
    refs = {(r["endpoint"], r["item_id"]) for r in p["_source_refs"]}
    assert ("articles", "100") in refs
    assert ("article_detail", "100") in refs
    assert p["_cross_refs"]["cvid"] == "100"


def test_parsed_opus_post_fixture():
    p = _load("parsed_opus_post.json")
    assert p["_model_name"] == "opus_post"
    assert p["opus_id"] == "500001"
    assert p["markdown"]
    assert p["cover"]
    assert p["_cross_refs"]["opus_id"] == "500001"


def test_parsed_dynamic_event_fixture():
    p = _load("parsed_dynamic_event.json")
    assert p["_model_name"] == "dynamic_event"
    assert p["major_type"] == "MAJOR_TYPE_DRAW"
    assert p["image_urls"], "DRAW dynamic should carry image_urls"
    assert p["_cross_refs"]["dynamic_id"] == p["dynamic_id"]


def test_parsed_video_work_fixture():
    p = _load("parsed_video_work.json")
    assert p["_model_name"] == "video_work"
    assert p["bvid"].startswith("BV")
    assert p["pages"], "video_work golden should expose at least one page"
    assert p["tags"]
    assert p["stat"]["view"] > 0
    assert p["owner"]["mid"]
    assert p["_cross_refs"]["bvid"] == p["bvid"]


# -- round-trip the parsing goldens through from_dict ----------------------
#
# Beyond shape: the goldens must survive the same ``from_dict`` round-trip
# that production code uses, otherwise they'll silently desync from the
# dataclasses they're meant to mirror.

def test_article_golden_round_trips():
    from bili_unit.parsing.models.article import Article
    p = _load("parsed_article_post.json")
    art = Article.from_dict(p)
    redumped = art.to_dict()
    # Round-trip equality on the load-bearing fields (from_dict normalises
    # a few alias keys like ``cvid`` ↔ ``id`` so we compare the canonical
    # surface, not the raw dict).
    assert redumped["_cross_refs"]["cvid"] == "100"
    assert redumped["markdown"] == p["markdown"]
    assert redumped["stats"] == p["stats"]


def test_opus_golden_round_trips():
    from bili_unit.parsing.models.opus import OpusPost
    p = _load("parsed_opus_post.json")
    op = OpusPost.from_dict(p)
    redumped = op.to_dict()
    assert redumped["_cross_refs"]["opus_id"] == "500001"
    assert redumped["markdown"] == p["markdown"]


def test_dynamic_golden_round_trips():
    from bili_unit.parsing.models.dynamic import DynamicPost
    p = _load("parsed_dynamic_event.json")
    dp = DynamicPost.from_dict(p)
    assert dp.major_type == "MAJOR_TYPE_DRAW"
    assert dp.image_urls == p["image_urls"]


def test_video_work_golden_round_trips():
    from bili_unit.parsing.models.video_detail import VideoDetail
    p = _load("parsed_video_work.json")
    vd = VideoDetail.from_dict(p)
    assert vd.bvid == p["bvid"]
    assert len(vd.pages) == len(p["pages"])
    assert vd.tags == p["tags"]
