# Tests for the W3.1 ``is_complete`` property on each parsing model.
#
# Each model exposes a computed ``is_complete`` property (not a stored
# field).  ``to_dict()`` materialises the current value; ``from_dict()``
# ignores any persisted value and recomputes from the rebuilt state.

from __future__ import annotations

from bili_unit.parsing.models.article import Article
from bili_unit.parsing.models.content_post import (
    ContentPost,
    CrossRefs,
    SourceRef,
)
from bili_unit.parsing.models.dynamic import DynamicPost
from bili_unit.parsing.models.opus import OpusPost
from bili_unit.parsing.models.up_profile import UpProfile
from bili_unit.parsing.models.video_detail import VideoDetail

# ---------------------------------------------------------------------------
# UpProfile
# ---------------------------------------------------------------------------

def test_up_profile_complete_with_three_required_endpoints():
    """All three required endpoints (user_info / relation_info / up_stat)
    contributing source_refs makes the profile complete."""
    p = UpProfile(
        mid=1,
        source_refs=[
            SourceRef("user_info", "1"),
            SourceRef("relation_info", "1"),
            SourceRef("up_stat", "1"),
        ],
    )
    assert p.is_complete is True


def test_up_profile_incomplete_when_one_required_missing():
    """Drop any of the three required endpoints → not complete."""
    p = UpProfile(
        mid=1,
        source_refs=[
            SourceRef("user_info", "1"),
            SourceRef("relation_info", "1"),
            # up_stat missing
        ],
    )
    assert p.is_complete is False


def test_up_profile_overview_stat_does_not_affect_completeness():
    """``overview_stat`` is optional — present or absent it's a no-op
    for ``is_complete``."""
    p_with = UpProfile(
        mid=1,
        source_refs=[
            SourceRef("user_info", "1"),
            SourceRef("relation_info", "1"),
            SourceRef("up_stat", "1"),
            SourceRef("overview_stat", "1"),
        ],
    )
    p_without = UpProfile(
        mid=1,
        source_refs=[
            SourceRef("user_info", "1"),
            SourceRef("relation_info", "1"),
            SourceRef("up_stat", "1"),
        ],
    )
    assert p_with.is_complete is True
    assert p_without.is_complete is True


# ---------------------------------------------------------------------------
# VideoDetail
# ---------------------------------------------------------------------------

def test_video_detail_complete_with_video_detail_ref_and_bvid():
    v = VideoDetail(
        bvid="BV1xx",
        source_refs=[SourceRef("video_detail", "BV1xx")],
    )
    assert v.is_complete is True


def test_video_detail_incomplete_when_no_bvid():
    v = VideoDetail(
        bvid="",
        source_refs=[SourceRef("video_detail", "")],
    )
    assert v.is_complete is False


def test_video_detail_incomplete_when_no_source_ref():
    v = VideoDetail(bvid="BV1xx", source_refs=[])
    assert v.is_complete is False


# ---------------------------------------------------------------------------
# Article
# ---------------------------------------------------------------------------

def test_article_complete_with_detail_ref():
    a = Article(
        id="100",
        source_refs=[
            SourceRef("articles", "100"),
            SourceRef("article_detail", "100"),
        ],
    )
    assert a.is_complete is True


def test_article_incomplete_with_only_listing_ref():
    """Listing-only data lacks the markdown body → incomplete."""
    a = Article(
        id="100",
        source_refs=[SourceRef("articles", "100")],
    )
    assert a.is_complete is False


# ---------------------------------------------------------------------------
# OpusPost
# ---------------------------------------------------------------------------

def test_opus_complete_with_detail_ref():
    o = OpusPost(
        id="500",
        source_refs=[
            SourceRef("opus", "500"),
            SourceRef("opus_detail", "500"),
        ],
    )
    assert o.is_complete is True


def test_opus_incomplete_with_only_listing_ref():
    o = OpusPost(
        id="500",
        source_refs=[SourceRef("opus", "500")],
    )
    assert o.is_complete is False


# ---------------------------------------------------------------------------
# DynamicPost
# ---------------------------------------------------------------------------

def test_dynamic_complete_when_id_str_set():
    d = DynamicPost(id_str="9999", dynamic_id="9999")
    assert d.is_complete is True


def test_dynamic_incomplete_when_id_str_blank():
    d = DynamicPost(id_str="", dynamic_id="")
    assert d.is_complete is False


# ---------------------------------------------------------------------------
# ContentPost
# ---------------------------------------------------------------------------

def test_content_post_article_complete_when_markdown_set():
    c = ContentPost(
        content_key="article:100", kind="article",
        title="t", markdown="# body",
    )
    assert c.is_complete is True


def test_content_post_article_incomplete_without_markdown():
    c = ContentPost(content_key="article:100", kind="article", title="t")
    assert c.is_complete is False


def test_content_post_opus_complete_when_markdown_set():
    c = ContentPost(
        content_key="opus:500", kind="opus", markdown="**body**",
    )
    assert c.is_complete is True


def test_content_post_video_complete_with_title_and_bvid():
    c = ContentPost(
        content_key="video:BV1xx",
        kind="video",
        title="hello",
        cross_refs=CrossRefs(bvid="BV1xx"),
    )
    assert c.is_complete is True


def test_content_post_video_incomplete_without_bvid():
    c = ContentPost(content_key="video:?", kind="video", title="hello")
    assert c.is_complete is False


def test_content_post_video_incomplete_without_title():
    c = ContentPost(
        content_key="video:BV1xx",
        kind="video",
        cross_refs=CrossRefs(bvid="BV1xx"),
    )
    assert c.is_complete is False


def test_content_post_dynamic_complete_with_text():
    c = ContentPost(
        content_key="dynamic:1", kind="dynamic_draw", text="hi there",
    )
    assert c.is_complete is True


def test_content_post_dynamic_complete_with_images():
    c = ContentPost(
        content_key="dynamic:1", kind="dynamic_draw",
        images=["https://i0.hdslb.com/x.jpg"],
    )
    assert c.is_complete is True


def test_content_post_dynamic_incomplete_when_empty():
    c = ContentPost(content_key="dynamic:1", kind="dynamic_draw")
    assert c.is_complete is False


# ---------------------------------------------------------------------------
# Round-trip: to_dict materialises is_complete; from_dict ignores it.
# ---------------------------------------------------------------------------

def test_up_profile_roundtrip_carries_is_complete():
    p = UpProfile(
        mid=1,
        source_refs=[
            SourceRef("user_info", "1"),
            SourceRef("relation_info", "1"),
            SourceRef("up_stat", "1"),
        ],
    )
    d = p.to_dict()
    assert d["is_complete"] is True
    revived = UpProfile.from_dict(d)
    assert revived.is_complete is True


def test_video_detail_roundtrip_carries_is_complete():
    v = VideoDetail(
        bvid="BV1xx",
        source_refs=[SourceRef("video_detail", "BV1xx")],
    )
    d = v.to_dict()
    assert d["is_complete"] is True
    revived = VideoDetail.from_dict(d)
    assert revived.is_complete is True


def test_article_roundtrip_carries_is_complete():
    a = Article(
        id="100",
        source_refs=[
            SourceRef("articles", "100"),
            SourceRef("article_detail", "100"),
        ],
    )
    d = a.to_dict()
    assert d["is_complete"] is True
    revived = Article.from_dict(d)
    assert revived.is_complete is True


def test_opus_roundtrip_carries_is_complete():
    o = OpusPost(
        id="500",
        source_refs=[
            SourceRef("opus", "500"),
            SourceRef("opus_detail", "500"),
        ],
    )
    d = o.to_dict()
    assert d["is_complete"] is True
    revived = OpusPost.from_dict(d)
    assert revived.is_complete is True


def test_dynamic_roundtrip_carries_is_complete():
    dyn = DynamicPost(id_str="9999", dynamic_id="9999")
    d = dyn.to_dict()
    assert d["is_complete"] is True
    revived = DynamicPost.from_dict(d)
    assert revived.is_complete is True


def test_content_post_roundtrip_carries_is_complete():
    c = ContentPost(
        content_key="article:100", kind="article",
        title="t", markdown="# body",
    )
    d = c.to_dict()
    assert d["is_complete"] is True
    revived = ContentPost.from_dict(d)
    assert revived.is_complete is True


def test_from_dict_recomputes_is_complete_ignoring_persisted_value():
    """Even if a stale ``is_complete`` is persisted, ``from_dict`` does not
    read it — the rebuilt object recomputes from its actual state."""
    a = Article(id="100", source_refs=[SourceRef("articles", "100")])
    d = a.to_dict()
    # Tamper with the persisted flag — should be ignored on rebuild.
    d["is_complete"] = True
    revived = Article.from_dict(d)
    # Article only has the listing source_ref → still incomplete.
    assert revived.is_complete is False
