from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key
from bili_unit.parsing.materializer import ParsingMaterializer
from bili_unit.parsing.models.content_post import (
    ContentPost,
    CrossRefs,
    SourceRef,
    content_key_for_refs,
)
from bili_unit.parsing.query import ParsingQuery
from bili_unit.parsing.selectors import (
    merge_content_posts,
    video_posts_from_parsed,
)


@pytest_asyncio.fixture
async def parsing_store(tmp_path):
    store = ParsingDataStore(tmp_path / "parsing")
    await store.open()
    yield store
    await store.close()


def _video_work_dict(bvid: str = "BV1abcdef123") -> dict:
    return {
        "_model_name": "video_work",
        "_schema_version": 1,
        "bvid": bvid,
        "aid": 9999001,
        "title": "Hands-on Rust async tutorial",
        "desc": "Walk through tokio + select! patterns.",
        "duration": 1830,
        "ctime": 1700300000,
        "pubdate": 1700300600,
        "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
        "pages": [],
        "tags": ["Rust", "async"],
        "stat": {
            "view": 5000,
            "danmaku": 30,
            "reply": 12,
            "favorite": 240,
            "coin": 60,
            "share": 25,
            "like": 320,
        },
        "owner": {"mid": 7777, "name": "rustacean", "face": ""},
        "rights": {},
        "subtitle": {},
        "label": {},
        "pic_local": "",
        "_source_refs": [{"endpoint": "video_detail", "item_id": bvid}],
        "_cross_refs": {
            "cvid": None,
            "opus_id": None,
            "dynamic_id": None,
            "bvid": bvid,
        },
    }


def test_video_posts_from_parsed_yields_video_kind_and_video_content_key():
    posts = video_posts_from_parsed([_video_work_dict("BV1abcdef123")])

    assert len(posts) == 1
    post = posts[0]
    assert post.kind == "video"
    assert post.content_key == "video:BV1abcdef123"
    assert post.cross_refs.bvid == "BV1abcdef123"
    assert post.title == "Hands-on Rust async tutorial"
    assert post.summary == "Walk through tokio + select! patterns."
    assert post.text == "Walk through tokio + select! patterns."
    assert post.images == ["https://i0.hdslb.com/bfs/archive/cover.jpg"]
    assert post.pub_time == 1700300600
    assert post.stats["view"] == 5000
    assert {(r.endpoint, r.item_id) for r in post.source_refs} == {
        ("video_detail", "BV1abcdef123"),
    }


def test_video_posts_from_parsed_skips_entries_without_bvid():
    bad = dict(_video_work_dict())
    bad["bvid"] = ""
    bad["_cross_refs"] = {"cvid": None, "opus_id": None, "dynamic_id": None, "bvid": None}
    assert video_posts_from_parsed([bad]) == []


def test_video_post_alone_merges_to_single_content_post():
    [post] = video_posts_from_parsed([_video_work_dict("BV1abcdef123")])
    merged = merge_content_posts([post])

    assert len(merged) == 1
    assert merged[0].content_key == "video:BV1abcdef123"
    assert merged[0].kind == "video"


def test_archive_dynamic_and_video_merge_to_video_kind_with_both_sources():
    bvid = "BV1abcdef123"
    [video_post] = video_posts_from_parsed([_video_work_dict(bvid)])

    # Build an archive-style dynamic candidate the way select_dynamic_content
    # would for a MAJOR_TYPE_ARCHIVE entry: same bvid, plus the dynamic_id.
    dynamic_id = "dyn_archive_900"
    archive_post = ContentPost(
        content_key=content_key_for_refs(CrossRefs(dynamic_id=dynamic_id, bvid=bvid)),
        kind="video",
        title="Archive announcement",
        summary="Check my new tutorial",
        text="Check my new tutorial",
        images=["https://i0.hdslb.com/bfs/dynamic/archive_cover.jpg"],
        pub_time=1700300700,
        stats={},
        source_refs=[SourceRef("dynamics", dynamic_id)],
        cross_refs=CrossRefs(dynamic_id=dynamic_id, bvid=bvid),
    )

    merged = merge_content_posts([video_post, archive_post])

    assert len(merged) == 1
    out = merged[0]
    assert out.kind == "video"
    assert out.content_key == f"video:{bvid}"
    assert out.cross_refs.bvid == bvid
    assert out.cross_refs.dynamic_id == dynamic_id
    endpoints = {(r.endpoint, r.item_id) for r in out.source_refs}
    assert ("video_detail", bvid) in endpoints
    assert ("dynamics", dynamic_id) in endpoints


def test_dynamic_only_post_keeps_dynamic_kind_when_no_video_candidate():
    # Sanity: without a video candidate to absorb it, a kind="video" archive
    # post still gets the video:{bvid} key (bvid > dynamic_id) but stands alone.
    bvid = "BV1xx"
    archive_post = ContentPost(
        content_key=content_key_for_refs(CrossRefs(dynamic_id="dyn1", bvid=bvid)),
        kind="video",
        title="Archive only",
        summary="",
        text="",
        images=[],
        pub_time=1700000000,
        stats={},
        source_refs=[SourceRef("dynamics", "dyn1")],
        cross_refs=CrossRefs(dynamic_id="dyn1", bvid=bvid),
    )
    [merged] = merge_content_posts([archive_post])
    assert merged.content_key == f"video:{bvid}"
    assert merged.kind == "video"


def test_content_key_priority_cvid_over_opus_over_bvid_over_dynamic():
    assert content_key_for_refs(CrossRefs(cvid="A")) == "article:A"
    assert content_key_for_refs(CrossRefs(cvid="A", opus_id="B")) == "article:A"
    assert content_key_for_refs(CrossRefs(opus_id="B")) == "opus:B"
    assert content_key_for_refs(CrossRefs(opus_id="B", bvid="C")) == "opus:B"
    assert content_key_for_refs(CrossRefs(bvid="C")) == "video:C"
    assert content_key_for_refs(CrossRefs(bvid="C", dynamic_id="D")) == "video:C"
    assert content_key_for_refs(CrossRefs(dynamic_id="D")) == "dynamic:D"
    assert content_key_for_refs(CrossRefs(), fallback="x") == "x"


def test_content_post_sort_order_article_opus_video_dynamic():
    posts = [
        ContentPost(
            content_key="dynamic:d1",
            kind="dynamic_draw",
            cross_refs=CrossRefs(dynamic_id="d1"),
        ),
        ContentPost(
            content_key="video:BVx",
            kind="video",
            cross_refs=CrossRefs(bvid="BVx"),
        ),
        ContentPost(
            content_key="opus:200",
            kind="opus",
            cross_refs=CrossRefs(opus_id="200"),
        ),
        ContentPost(
            content_key="article:100",
            kind="article",
            cross_refs=CrossRefs(cvid="100"),
        ),
    ]
    merged = merge_content_posts(posts)
    assert [p.content_key for p in merged] == [
        "article:100",
        "opus:200",
        "video:BVx",
        "dynamic:d1",
    ]


@pytest.mark.asyncio
async def test_materializer_emits_video_content_post_from_parsed_store(parsing_store):
    uid = 9090
    bvid = "BV1abcdef123"

    # Pre-populate the parsing store with a video_work entry so that
    # _content_candidates_from_parsed picks it up.
    await parsing_store.put(
        _item_key(uid, "video_work", bvid),
        _video_work_dict(bvid),
    )

    # Also seed a dynamic_event referencing the same bvid; ensure the merged
    # content_post stays kind="video" and content_key="video:{bvid}".
    await parsing_store.put(
        _item_key(uid, "dynamic_event", "dyn_archive_900"),
        {
            "_model_name": "dynamic_event",
            "_schema_version": 1,
            "id_str": "dyn_archive_900",
            "dynamic_id": "dyn_archive_900",
            "type": "DYNAMIC_TYPE_AV",
            "text": "Watch my new tutorial",
            "timestamp": 1700300700,
            "pub_time": 1700300700,
            "major": {"type": "MAJOR_TYPE_ARCHIVE", "bvid": bvid},
            "major_type": "MAJOR_TYPE_ARCHIVE",
            "target_ref": f"video:{bvid}",
            "forwarded_ref": None,
            "forwarded": None,
            "image_urls": [],
            "image_locals": [],
            "_source_refs": [{"endpoint": "dynamics", "item_id": "dyn_archive_900"}],
            "_cross_refs": {
                "cvid": None,
                "opus_id": None,
                "dynamic_id": "dyn_archive_900",
                "bvid": bvid,
            },
        },
    )

    fetch_query = MagicMock()
    fetch_query.get_endpoint = AsyncMock(return_value=None)
    fetch_query.list_fanout_payloads = AsyncMock(return_value={})
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "content_post", "full")

    assert count == 1
    qry = ParsingQuery(parsing_store)
    rows = await qry.list_items(uid, "content_post")
    by_key = {row["content_key"]: row for row in rows}
    assert set(by_key) == {f"video:{bvid}"}
    row = by_key[f"video:{bvid}"]
    assert row["kind"] == "video"
    assert row["title"] == "Hands-on Rust async tutorial"
    assert row["_cross_refs"]["bvid"] == bvid
    endpoints = {
        (ref["endpoint"], ref["item_id"]) for ref in row["_source_refs"]
    }
    # video_detail must be present; the archive dynamic also contributes its source ref
    assert ("video_detail", bvid) in endpoints


@pytest.mark.asyncio
async def test_materializer_emits_video_post_even_without_dynamic(parsing_store):
    uid = 9191
    bvid = "BV2standalone"

    await parsing_store.put(
        _item_key(uid, "video_work", bvid),
        _video_work_dict(bvid),
    )

    fetch_query = MagicMock()
    fetch_query.get_endpoint = AsyncMock(return_value=None)
    fetch_query.list_fanout_payloads = AsyncMock(return_value={})
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "content_post", "full")
    assert count == 1

    qry = ParsingQuery(parsing_store)
    rows = await qry.list_items(uid, "content_post")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "video"
    assert row["content_key"] == f"video:{bvid}"
