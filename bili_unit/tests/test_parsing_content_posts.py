from __future__ import annotations

from bili_unit.parsing.models.content_post import (
    ContentPost,
    CrossRefs,
    SourceRef,
    content_post_item_id,
)
from bili_unit.parsing.selectors.article import select_article_posts
from bili_unit.parsing.selectors.dynamic import select_dynamic_content, select_dynamic_events
from bili_unit.parsing.selectors.merge import merge_content_posts
from bili_unit.parsing.selectors.opus import select_opus_posts


def test_content_post_roundtrip_and_item_id():
    post = ContentPost(
        content_key="article:100",
        kind="article",
        title="Title",
        summary="Summary",
        text="Text",
        markdown="# Title",
        images=["https://example.com/a.jpg"],
        pub_time=1700000000,
        stats={"view": 10},
        source_refs=[SourceRef("articles", "100")],
        cross_refs=CrossRefs(cvid="100", opus_id="200", dynamic_id="300"),
    )

    restored = ContentPost.from_dict(post.to_dict())

    assert restored.item_id == "article~100"
    assert content_post_item_id("dynamic:abc") == "dynamic~abc"
    assert restored.to_dict() == post.to_dict()


def test_select_article_posts_with_detail_and_readlist_refs():
    raw_articles = {
        "pages": [
            {
                "articles": [
                    {
                        "id": 100,
                        "title": "List title",
                        "summary": "List summary",
                        "image_urls": ["https://example.com/list.jpg"],
                        "stats": {"view": 1},
                        "ctime": 1700000000,
                    }
                ]
            }
        ]
    }
    details = {
        "100": {
            "info": {
                "id": 100,
                "title": "Detail title",
                "summary": "Detail summary",
                "banner_url": "https://example.com/detail.jpg",
                "stats": {"view": 2},
            },
            "markdown": "# Detail body",
            "content_json": [{"text": "Detail body"}],
        }
    }
    list_details = {
        "rl1": {
            "list": {"id": "rl1", "name": "Readlist"},
            "articles": [{"id": 100}],
        }
    }

    posts = select_article_posts(raw_articles, details, list_details)

    assert len(posts) == 1
    post = posts[0]
    assert post.content_key == "article:100"
    assert post.kind == "article"
    assert post.title == "Detail title"
    assert post.summary == "Detail summary"
    assert post.text == "Detail body"
    assert post.markdown == "# Detail body"
    assert post.images == ["https://example.com/detail.jpg", "https://example.com/list.jpg"]
    assert post.pub_time == 1700000000
    assert post.stats == {"view": 2}
    assert post.cross_refs.to_dict() == {
        "cvid": "100",
        "opus_id": None,
        "dynamic_id": None,
        "bvid": None,
    }
    assert [ref.to_dict() for ref in post.source_refs] == [
        {"endpoint": "articles", "item_id": "100"},
        {"endpoint": "article_detail", "item_id": "100"},
        {"endpoint": "article_list_detail", "item_id": "rl1"},
    ]


def test_select_opus_posts_with_detail():
    raw_opus = {
        "pages": [
            {
                "items": [
                    {
                        "opus_id": "200",
                        "title": "List opus",
                        "summary": "List summary",
                        "cover": "https://example.com/cover.jpg",
                        "stats": {"like": 1},
                        "pub_time": 1700000010,
                        "modules": {
                            "module_dynamic": {
                                "major": {
                                    "type": "MAJOR_TYPE_OPUS",
                                    "opus": {
                                        "summary": {"text": "Module summary"},
                                        "pics": [{"url": "https://example.com/list.jpg"}],
                                    },
                                },
                            },
                        },
                    }
                ]
            }
        ]
    }
    details = {
        "200": {
            "info": {
                "item": {
                    "title": "Detail opus",
                    "summary": "Detail summary",
                    "stats": {"like": 2},
                    "pub_time": 1700000020,
                }
            },
            "markdown": "# Opus detail",
            "images": [{"url": "https://example.com/detail.jpg"}],
        }
    }

    posts = select_opus_posts(raw_opus, details)

    assert len(posts) == 1
    post = posts[0]
    assert post.content_key == "opus:200"
    assert post.kind == "opus"
    assert post.title == "Detail opus"
    assert post.summary == "Detail summary"
    assert post.text == "# Opus detail"
    assert post.markdown == "# Opus detail"
    assert post.images == [
        "https://example.com/detail.jpg",
        "https://example.com/list.jpg",
        "https://example.com/cover.jpg",
    ]
    assert post.pub_time == 1700000020
    assert post.stats == {"like": 2}
    assert [ref.to_dict() for ref in post.source_refs] == [
        {"endpoint": "opus", "item_id": "200"},
        {"endpoint": "opus_detail", "item_id": "200"},
    ]


def test_select_dynamic_article_opus_video_draw_and_forward():
    raw_dynamics = {
        "pages": [
            {
                "items": [
                    {
                        "id_str": "dyn_article",
                        "type": "DYNAMIC_TYPE_ARTICLE",
                        "modules": {
                            "module_author": {"pub_ts": 1700000100},
                            "module_dynamic": {
                                "desc": {"text": "Article dynamic text"},
                                "major": {
                                    "type": "MAJOR_TYPE_ARTICLE",
                                    "article": {
                                        "id": 100,
                                        "title": "Article title",
                                        "desc": "Article desc",
                                        "covers": ["https://example.com/article.jpg"],
                                    },
                                },
                            },
                        },
                    },
                    {
                        "id_str": "dyn_opus",
                        "type": "DYNAMIC_TYPE_OPUS",
                        "modules": {
                            "module_author": {"pub_ts": 1700000200},
                            "module_dynamic": {
                                "desc": {"text": "Opus dynamic text"},
                                "major": {
                                    "type": "MAJOR_TYPE_OPUS",
                                    "opus": {
                                        "jump_url": "https://www.bilibili.com/opus/200",
                                        "summary": {"text": "Opus summary"},
                                        "pics": [{"url": "https://example.com/opus.jpg"}],
                                    },
                                },
                            },
                        },
                    },
                    {
                        "id_str": "dyn_video",
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_dynamic": {
                                "desc": {"text": "Video dynamic text"},
                                "major": {
                                    "type": "MAJOR_TYPE_ARCHIVE",
                                    "archive": {
                                        "bvid": "BV1xx",
                                        "title": "Video title",
                                        "desc": "Video desc",
                                        "cover": "https://example.com/video.jpg",
                                    },
                                },
                            },
                        },
                    },
                    {
                        "id_str": "dyn_draw",
                        "type": "DYNAMIC_TYPE_DRAW",
                        "modules": {
                            "module_dynamic": {
                                "desc": {"text": "Draw text"},
                                "major": {
                                    "type": "MAJOR_TYPE_DRAW",
                                    "draw": {"items": [{"src": "https://example.com/draw.jpg"}]},
                                },
                            },
                        },
                    },
                    {
                        "id_str": "dyn_forward",
                        "type": "DYNAMIC_TYPE_FORWARD",
                        "modules": {
                            "module_dynamic": {"desc": {"text": "Forward text"}},
                        },
                        "orig": {
                            "id_str": "dyn_orig",
                            "type": "DYNAMIC_TYPE_DRAW",
                            "modules": {
                                "module_dynamic": {
                                    "desc": {"text": "Original draw"},
                                    "major": {
                                        "type": "MAJOR_TYPE_DRAW",
                                        "draw": {"items": [{"src": "https://example.com/orig.jpg"}]},
                                    },
                                },
                            },
                        },
                    },
                ]
            }
        ]
    }

    events = select_dynamic_events(raw_dynamics)
    posts = select_dynamic_content(raw_dynamics)
    posts_by_key = {post.content_key: post for post in posts}

    assert len(events) == 5
    assert events[0].target_ref == "article:100"
    assert events[0].cross_refs.cvid == "100"
    assert events[1].target_ref == "opus:200"
    assert events[1].cross_refs.opus_id == "200"
    assert events[2].target_ref == "video:BV1xx"
    assert events[2].cross_refs.bvid == "BV1xx"
    assert events[3].target_ref == "dynamic:dyn_draw"
    assert events[4].forwarded_ref == "dynamic:dyn_orig"
    assert events[4].target_ref == "dynamic:dyn_orig"

    assert set(posts_by_key) == {
        "article:100",
        "opus:200",
        "dynamic:dyn_draw",
        "dynamic:dyn_forward",
        "dynamic:dyn_orig",
    }
    assert posts_by_key["article:100"].kind == "article"
    assert posts_by_key["article:100"].cross_refs.dynamic_id == "dyn_article"
    assert posts_by_key["opus:200"].kind == "opus"
    assert all(post.kind != "video" for post in posts)
    assert posts_by_key["dynamic:dyn_draw"].kind == "dynamic_draw"
    assert posts_by_key["dynamic:dyn_draw"].images == ["https://example.com/draw.jpg"]
    assert posts_by_key["dynamic:dyn_forward"].kind == "forward"
    assert posts_by_key["dynamic:dyn_forward"].text == "Forward text"
    assert posts_by_key["dynamic:dyn_orig"].kind == "dynamic_draw"
    assert posts_by_key["dynamic:dyn_orig"].images == ["https://example.com/orig.jpg"]


def test_merge_content_posts_dedups_sources_and_prefers_article_then_opus_then_dynamic_detail_text():
    dynamic_article = ContentPost(
        content_key="article:100",
        kind="article",
        title="Dynamic article title",
        summary="Dynamic article summary",
        text="Dynamic text",
        images=["dynamic.jpg"],
        pub_time=1700000100,
        stats={"view": 1},
        source_refs=[SourceRef("dynamics", "dyn1")],
        cross_refs=CrossRefs(cvid="100", dynamic_id="dyn1"),
    )
    article_detail = ContentPost(
        content_key="article:100",
        kind="article",
        title="Detail title",
        summary="Detail summary",
        text="Detail text",
        markdown="# Detail",
        images=["detail.jpg", "dynamic.jpg"],
        pub_time=1700000000,
        stats={"view": 2},
        source_refs=[SourceRef("articles", "100"), SourceRef("article_detail", "100"), SourceRef("dynamics", "dyn1")],
        cross_refs=CrossRefs(cvid="100"),
    )
    dynamic_opus = ContentPost(
        content_key="opus:200",
        kind="opus",
        title="Dynamic opus",
        text="Dynamic opus text",
        source_refs=[SourceRef("dynamics", "dyn2")],
        cross_refs=CrossRefs(opus_id="200", dynamic_id="dyn2"),
    )
    opus_detail = ContentPost(
        content_key="opus:200",
        kind="opus",
        title="Detail opus",
        text="Detail opus text",
        markdown="# Opus detail",
        source_refs=[SourceRef("opus", "200"), SourceRef("opus_detail", "200")],
        cross_refs=CrossRefs(opus_id="200"),
    )
    dynamic_only = ContentPost(
        content_key="dynamic:dyn3",
        kind="dynamic_draw",
        text="Draw",
        source_refs=[SourceRef("dynamics", "dyn3")],
        cross_refs=CrossRefs(dynamic_id="dyn3"),
    )

    merged = merge_content_posts([dynamic_article, article_detail, dynamic_opus, opus_detail, dynamic_only])
    merged_by_key = {post.content_key: post for post in merged}

    assert set(merged_by_key) == {"article:100", "opus:200", "dynamic:dyn3"}
    article = merged_by_key["article:100"]
    assert article.title == "Detail title"
    assert article.summary == "Detail summary"
    assert article.text == "Detail text"
    assert article.markdown == "# Detail"
    assert article.images == ["dynamic.jpg", "detail.jpg"]
    assert article.stats == {"view": 1}
    assert article.cross_refs.dynamic_id == "dyn1"
    assert [ref.to_dict() for ref in article.source_refs] == [
        {"endpoint": "dynamics", "item_id": "dyn1"},
        {"endpoint": "articles", "item_id": "100"},
        {"endpoint": "article_detail", "item_id": "100"},
    ]

    opus = merged_by_key["opus:200"]
    assert opus.title == "Detail opus"
    assert opus.text == "Detail opus text"
    assert opus.markdown == "# Opus detail"
    assert opus.cross_refs.dynamic_id == "dyn2"
    assert merged_by_key["dynamic:dyn3"].text == "Draw"
