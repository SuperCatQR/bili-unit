# tests for bili_unit/processing — transform handlers (pure functions).

from bili_unit.processing.transform import (
    HANDLERS,
    articles,
    dynamics,
    get_handler,
    opus,
    user_profile,
    video_metadata,
)

# ---------- video_metadata ----------------------------------------------

def test_video_metadata_handler_extract_and_transform():
    raw = {
        "info": {
            "bvid": "BV1xx",
            "aid": 12345,
            "title": "Hello",
            "desc": "long description",
            "duration": 600,
            "ctime": 1700000000,
            "pubdate": 1700000000,
            "pages": [
                {"cid": 11, "part": "P1", "duration": 300, "dimension": {"w": 1920}},
                {"cid": 12, "part": "P2", "duration": 300, "dimension": {"w": 1920}},
            ],
            "stat": {
                "view": 10000, "danmaku": 50, "reply": 5,
                "favorite": 100, "coin": 30, "share": 12, "like": 999,
            },
            "owner": {"mid": 7, "name": "U", "face": "https://x"},
            "rights": {"download": 0},
            "subtitle": {"list": []},
            "label": {"name": "test"},
        },
        "tags": [
            {"tag_name": "hello"},
            {"tag_name": "world"},
            "string-tag",
            {"name": "ignored-no-tag_name"},
        ],
    }
    h = video_metadata.HANDLER
    items = h.extract_items({"video_detail": raw})
    assert len(items) == 1
    assert items[0].item_type == "video_metadata"
    assert items[0].item_id == "BV1xx"

    out = h.transform(items[0])
    assert out["bvid"] == "BV1xx"
    assert out["aid"] == 12345
    assert out["title"] == "Hello"
    assert out["duration"] == 600
    assert len(out["pages"]) == 2
    assert out["pages"][0]["cid"] == 11
    assert out["tags"] == ["hello", "world", "string-tag"]
    assert out["stat"]["view"] == 10000
    assert out["stat"]["like"] == 999
    assert out["owner"] == {"mid": 7, "name": "U", "face": "https://x"}
    assert out["rights"] == {"download": 0}


def test_video_metadata_handler_handles_missing_fields():
    raw = {"info": {"bvid": "BV2"}, "tags": []}
    h = video_metadata.HANDLER
    items = h.extract_items({"video_detail": raw})
    assert len(items) == 1
    out = h.transform(items[0])
    assert out["bvid"] == "BV2"
    assert out["aid"] is None
    assert out["title"] == ""
    assert out["pages"] == []
    assert out["tags"] == []
    assert out["stat"]["view"] == 0
    assert out["owner"]["mid"] is None


def test_video_metadata_handler_skips_when_no_bvid():
    h = video_metadata.HANDLER
    assert h.extract_items({"video_detail": {"info": {}}}) == []
    assert h.extract_items({}) == []
    assert h.extract_items({"video_detail": "not-a-dict"}) == []


# ---------- dynamics ------------------------------------------------------

def test_dynamics_handler_word_type():
    raw = {
        "pages": [{"items": [{
            "id_str": "100",
            "type": "DYNAMIC_TYPE_WORD",
            "modules": {
                "module_author": {"pub_ts": "1718000000"},
                "module_dynamic": {"desc": {"text": "plain word"}, "major": None},
            },
        }]}],
    }
    h = dynamics.HANDLER
    items = h.extract_items({"dynamics": raw})
    assert [it.item_id for it in items] == ["100"]
    out = h.transform(items[0])
    assert out["id_str"] == "100"
    assert out["type"] == "DYNAMIC_TYPE_WORD"
    assert out["text"] == "plain word"
    assert out["timestamp"] == 1718000000
    assert out["major"] == {}
    assert out["forwarded"] is None


def test_dynamics_handler_draw_type_extracts_images():
    raw = {
        "pages": [{"items": [{
            "id_str": "200",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"pub_ts": "1718000001"},
                "module_dynamic": {
                    "desc": {"text": "图文动态"},
                    "major": {
                        "type": "MAJOR_TYPE_DRAW",
                        "draw": {"items": [
                            {"src": "https://img/1.jpg"},
                            {"src": "https://img/2.jpg"},
                        ]},
                    },
                },
            },
        }]}],
    }
    out = dynamics.HANDLER.transform(dynamics.HANDLER.extract_items({"dynamics": raw})[0])
    assert out["text"] == "图文动态"
    assert out["major"]["type"] == "MAJOR_TYPE_DRAW"
    assert out["major"]["images"] == ["https://img/1.jpg", "https://img/2.jpg"]


def test_dynamics_handler_archive_type():
    raw = {
        "pages": [{"items": [{
            "id_str": "300",
            "type": "DYNAMIC_TYPE_AV",
            "modules": {
                "module_author": {"pub_ts": "1718000002"},
                "module_dynamic": {
                    "desc": None,
                    "major": {
                        "type": "MAJOR_TYPE_ARCHIVE",
                        "archive": {
                            "bvid": "BV1xx", "aid": "12345",
                            "title": "Video Title", "desc": "video desc",
                            "duration_text": "10:00",
                            "jump_url": "//www.bilibili.com/video/BV1xx",
                            "cover": "https://cover/x.jpg",
                        },
                    },
                },
            },
        }]}],
    }
    out = dynamics.HANDLER.transform(dynamics.HANDLER.extract_items({"dynamics": raw})[0])
    assert out["text"] == ""
    assert out["major"]["bvid"] == "BV1xx"
    assert out["major"]["title"] == "Video Title"


def test_dynamics_handler_forward_type_includes_orig():
    raw = {
        "pages": [{"items": [{
            "id_str": "400",
            "type": "DYNAMIC_TYPE_FORWARD",
            "modules": {
                "module_author": {"pub_ts": "1718000003"},
                "module_dynamic": {"desc": {"text": "转发评论"}, "major": None},
            },
            "orig": {
                "id_str": "300",
                "type": "DYNAMIC_TYPE_AV",
                "modules": {
                    "module_author": {"pub_ts": "1718000002"},
                    "module_dynamic": {
                        "desc": None,
                        "major": {
                            "type": "MAJOR_TYPE_ARCHIVE",
                            "archive": {"bvid": "BV1xx", "title": "orig title"},
                        },
                    },
                },
            },
        }]}],
    }
    out = dynamics.HANDLER.transform(dynamics.HANDLER.extract_items({"dynamics": raw})[0])
    assert out["text"] == "转发评论"
    assert out["forwarded"] is not None
    assert out["forwarded"]["id_str"] == "300"
    assert out["forwarded"]["major"]["bvid"] == "BV1xx"
    assert out["forwarded"]["major"]["title"] == "orig title"


def test_dynamics_handler_module_list_shape_tolerated():
    raw = {
        "pages": [{"items": [{
            "id_str": "500", "type": "T",
            "modules": [
                {"module_author": {"pub_ts": "1700000000"}},
                {"module_dynamic": {"desc": {"text": "list shape"}, "major": None}},
            ],
        }]}],
    }
    out = dynamics.HANDLER.transform(dynamics.HANDLER.extract_items({"dynamics": raw})[0])
    assert out["text"] == "list shape"
    assert out["timestamp"] == 1700000000


def test_dynamics_handler_empty_payload():
    assert dynamics.HANDLER.extract_items({}) == []
    assert dynamics.HANDLER.extract_items({"dynamics": {}}) == []
    assert dynamics.HANDLER.extract_items({"dynamics": {"pages": []}}) == []


# ---------- articles ------------------------------------------------------

def test_articles_handler_extract_and_transform():
    raw = {
        "pages": [
            {
                "articles": [
                    {
                        "id": 555,
                        "title": "Hello",
                        "summary": "summary text",
                        "image_urls": ["https://a.jpg"],
                        "banner_url": "https://b.jpg",
                        "stats": {"view": 10, "like": 2},
                        "publish_time": 1700000000,
                    },
                    {
                        "id": 666,
                        "title": "Second",
                        "summary": "",
                        "stats": {},
                    },
                    {"title": "no-id-skipped"},
                ],
            },
        ],
    }
    h = articles.HANDLER
    items = h.extract_items({"articles": raw})
    assert [it.item_id for it in items] == ["555", "666"]

    out0 = h.transform(items[0])
    assert out0["id"] == "555"
    assert out0["title"] == "Hello"
    assert out0["image_urls"] == ["https://a.jpg", "https://b.jpg"]
    assert out0["stats"]["view"] == 10
    assert out0["stats"]["like"] == 2
    assert out0["stats"]["reply"] == 0  # default
    assert out0["ctime"] == 1700000000
    # No detail attached → empty markdown / content_json / zero word count.
    assert out0["markdown"] == ""
    assert out0["content_json"] == []
    assert out0["word_count"] == 0

    out1 = h.transform(items[1])
    assert out1["title"] == "Second"
    assert out1["stats"]["view"] == 0


def test_articles_handler_attaches_readlist_membership():
    """When article_list_detail raw_payloads are supplied, transform emits
    a per-article ``lists`` field with the readlist (文集) memberships."""
    raw_articles = {
        "pages": [
            {"articles": [
                {"id": 100, "title": "in-readlist"},
                {"id": 200, "title": "in-two-readlists"},
                {"id": 300, "title": "no-readlist"},
            ]},
        ],
    }
    raw_list_details = {
        "1043430": {
            "list": {"id": 1043430, "name": "【警戒追踪】"},
            "articles": [{"id": 100}, {"id": 200}],
        },
        "1043431": {
            "list": {"id": 1043431, "name": "【前哨速递】"},
            "articles": [{"id": 200}],
        },
    }
    h = articles.HANDLER
    items = h.extract_items({
        "articles": raw_articles,
        "article_list_detail": raw_list_details,
    })
    assert [it.item_id for it in items] == ["100", "200", "300"]

    out_in_one = h.transform(items[0])
    assert out_in_one["lists"] == [
        {"rlid": "1043430", "name": "【警戒追踪】"},
    ]

    out_in_two = h.transform(items[1])
    # 200 is in both readlists; order follows iteration over list_details dict
    rlids = {m["rlid"] for m in out_in_two["lists"]}
    assert rlids == {"1043430", "1043431"}
    names = {m["name"] for m in out_in_two["lists"]}
    assert names == {"【警戒追踪】", "【前哨速递】"}

    out_unaffiliated = h.transform(items[2])
    assert out_unaffiliated["lists"] == []


def test_articles_handler_lists_defaults_empty_when_absent():
    """Missing article_list_detail raw_payload → ``lists`` is empty list, not
    None / KeyError; back-compat with callers built before the enrichment."""
    raw = {"pages": [{"articles": [{"id": 1, "title": "t"}]}]}
    items = articles.HANDLER.extract_items({"articles": raw})
    out = articles.HANDLER.transform(items[0])
    assert out["lists"] == []


def test_articles_handler_attaches_article_detail():
    """When article_detail raw_payloads are supplied, transform emits markdown / content_json / word_count."""
    raw_articles = {
        "pages": [
            {"articles": [
                {"id": 100, "title": "with-body"},
                {"id": 200, "title": "no-detail"},
            ]},
        ],
    }
    raw_details = {
        "100": {
            "info": {"id": 100, "title": "with-body"},
            "markdown": "正文一段话。",
            "content_json": [{"type": "ParagraphNode", "text": "正文一段话。"}],
        },
        # 200 has no detail → fallback path
    }
    h = articles.HANDLER
    items = h.extract_items({
        "articles": raw_articles,
        "article_detail": raw_details,
    })
    assert [it.item_id for it in items] == ["100", "200"]

    enriched = h.transform(items[0])
    assert enriched["markdown"] == "正文一段话。"
    assert enriched["content_json"] == [
        {"type": "ParagraphNode", "text": "正文一段话。"},
    ]
    assert enriched["word_count"] == len("正文一段话。")

    fallback = h.transform(items[1])
    assert fallback["markdown"] == ""
    assert fallback["content_json"] == []
    assert fallback["word_count"] == 0


def test_articles_handler_tolerates_legacy_bare_dict_item_data():
    """Hand-crafted WorkItem with bare list-level dict still transforms correctly."""
    from bili_unit.processing.transform._base import WorkItem

    legacy_item = WorkItem(
        item_type="articles",
        item_id="900",
        item_data={
            "id": 900,
            "title": "legacy",
            "summary": "s",
            "stats": {"view": 1},
        },
    )
    out = articles.HANDLER.transform(legacy_item)
    assert out["id"] == "900"
    assert out["title"] == "legacy"
    assert out["markdown"] == ""


# ---------- opus ---------------------------------------------------------

def test_opus_handler_extract_and_transform_list_only():
    """List-level only (no opus_detail) — markdown / images empty, ctime / stats present."""
    raw = {
        "pages": [
            {
                "items": [
                    {
                        "opus_id": "100",
                        "title": "图文一",
                        "summary": "summary text",
                        "cover": "https://i0.hdslb.com/c.jpg",
                        "stats": {"view": 10, "like": 2},
                        "pub_time": 1700000000,
                        "jump_url": "//opus.bilibili.com/100",
                    },
                    {
                        "opus_id": 200,
                        "title": "图文二",
                        "summary": "",
                        "stats": {},
                    },
                    {"title": "no-id-skipped"},
                ],
            },
        ],
    }
    h = opus.HANDLER
    items = h.extract_items({"opus": raw})
    # opus_id may be int or str — extract_items coerces to str.
    assert [it.item_id for it in items] == ["100", "200"]

    out0 = h.transform(items[0])
    assert out0["id"] == "100"
    assert out0["title"] == "图文一"
    assert out0["summary"] == "summary text"
    assert out0["image_urls"] == ["https://i0.hdslb.com/c.jpg"]
    assert out0["stats"]["view"] == 10
    assert out0["stats"]["like"] == 2
    assert out0["stats"]["reply"] == 0  # default
    assert out0["ctime"] == 1700000000
    assert out0["jump_url"] == "//opus.bilibili.com/100"
    # No detail attached → empty markdown / images / zero word count.
    assert out0["markdown"] == ""
    assert out0["images"] == []
    assert out0["word_count"] == 0

    out1 = h.transform(items[1])
    assert out1["title"] == "图文二"
    assert out1["stats"]["view"] == 0
    assert out1["image_urls"] == []  # no cover, no body pics


def test_opus_handler_attaches_opus_detail():
    """When opus_detail raw_payloads are supplied, transform emits markdown / images / word_count."""
    raw_opus = {
        "pages": [
            {"items": [
                {"opus_id": "300", "title": "with-body"},
                {"opus_id": "400", "title": "no-detail"},
            ]},
        ],
    }
    raw_details = {
        "300": {
            "info": {"item": {"basic": {}, "modules": []}},
            "markdown": "正文一段话。",
            "images": [
                {"url": "https://i0.hdslb.com/x.jpg", "width": 100, "height": 80},
            ],
        },
        # 400 has no detail → fallback path
    }
    h = opus.HANDLER
    items = h.extract_items({
        "opus": raw_opus,
        "opus_detail": raw_details,
    })
    assert [it.item_id for it in items] == ["300", "400"]

    enriched = h.transform(items[0])
    assert enriched["markdown"] == "正文一段话。"
    assert enriched["images"] == [
        {"url": "https://i0.hdslb.com/x.jpg", "width": 100, "height": 80},
    ]
    assert enriched["image_urls"] == ["https://i0.hdslb.com/x.jpg"]
    assert enriched["word_count"] == len("正文一段话。")

    fallback = h.transform(items[1])
    assert fallback["markdown"] == ""
    assert fallback["images"] == []
    assert fallback["word_count"] == 0


def test_opus_handler_extracts_summary_from_modules_when_list_summary_empty():
    """If list-level ``summary`` is empty, fall back to module_dynamic.major.opus.summary.text."""
    raw = {
        "pages": [
            {"items": [
                {
                    "opus_id": "500",
                    "title": "modules-only",
                    "summary": "",
                    "modules": {
                        "module_dynamic": {
                            "major": {
                                "opus": {
                                    "summary": {"text": "fallback summary"},
                                    "pics": [
                                        {"url": "https://i0.hdslb.com/p1.jpg"},
                                        {"url": "https://i0.hdslb.com/p2.jpg"},
                                    ],
                                },
                            },
                        },
                    },
                },
            ]},
        ],
    }
    items = opus.HANDLER.extract_items({"opus": raw})
    out = opus.HANDLER.transform(items[0])
    assert out["summary"] == "fallback summary"
    # No detail → image_urls comes from list-level pics.
    assert out["image_urls"] == [
        "https://i0.hdslb.com/p1.jpg",
        "https://i0.hdslb.com/p2.jpg",
    ]


def test_opus_handler_tolerates_legacy_bare_dict_item_data():
    """Hand-crafted WorkItem with bare list-level dict still transforms correctly."""
    from bili_unit.processing.transform._base import WorkItem

    legacy_item = WorkItem(
        item_type="opus",
        item_id="900",
        item_data={
            "opus_id": 900,
            "title": "legacy",
            "summary": "s",
            "stats": {"view": 1},
        },
    )
    out = opus.HANDLER.transform(legacy_item)
    assert out["id"] == "900"
    assert out["title"] == "legacy"
    assert out["summary"] == "s"
    assert out["markdown"] == ""


def test_opus_handler_empty_payload():
    assert opus.HANDLER.extract_items({}) == []
    assert opus.HANDLER.extract_items({"opus": {}}) == []
    assert opus.HANDLER.extract_items({"opus": {"pages": []}}) == []


# ---------- registry -----------------------------------------------------

def test_registry_lookup_and_iter():
    assert "video_metadata" in HANDLERS
    assert "dynamics" in HANDLERS
    assert "articles" in HANDLERS
    assert "opus" in HANDLERS
    assert "user_profile" in HANDLERS
    assert get_handler("video_metadata") is video_metadata.HANDLER
    assert get_handler("dynamics") is dynamics.HANDLER
    assert get_handler("articles") is articles.HANDLER
    assert get_handler("opus") is opus.HANDLER
    assert get_handler("user_profile") is user_profile.HANDLER
    assert get_handler("nonexistent") is None
    names = HANDLERS.names()
    assert set(names) >= {"video_metadata", "dynamics", "articles", "opus", "user_profile"}
    assert len(HANDLERS) == 5


# ---------- user_profile -------------------------------------------------

def _user_info(**overrides):
    base = {
        "mid": 13991807,
        "name": "测试 UP",
        "sex": "男",
        "sign": "测试签名",
        "face": "https://i0.hdslb.com/u.jpg",
        "birthday": "06-11",
        "level": 6,
        "vip": {"type": 1, "status": 1, "label": {"text": "年度大会员"}},
        "jointime": 1500000000,
    }
    base.update(overrides)
    return base


def _relation_info(**overrides):
    base = {"following": 120, "follower": 50000, "whisper": 0, "black": 0}
    base.update(overrides)
    return base


def _up_stat(**overrides):
    base = {
        "archive": {"view": 12345678},
        "article": {"view": 1234},
        "likes": 654321,
    }
    base.update(overrides)
    return base


def _overview_stat(**overrides):
    base = {"video": 77, "article": 1, "opus": 64}
    base.update(overrides)
    return base


def test_user_profile_handler_full_happy_path():
    h = user_profile.HANDLER
    raw = {
        "user_info": _user_info(),
        "relation_info": _relation_info(),
        "up_stat": _up_stat(),
        "overview_stat": _overview_stat(),
    }
    items = h.extract_items(raw)
    assert len(items) == 1
    assert items[0].item_type == "user_profile"
    assert items[0].item_id == "13991807"

    out = h.transform(items[0])
    assert out["uid"] == 13991807
    assert out["name"] == "测试 UP"
    assert out["sex"] == "男"
    assert out["sign"] == "测试签名"
    assert out["avatar"] == "https://i0.hdslb.com/u.jpg"
    assert out["birthday"] == "06-11"
    assert out["level"] == 6
    assert out["vip"] == {"type": 1, "status": 1, "label": "年度大会员"}
    assert out["join_time"] == 1500000000
    assert out["social"] == {
        "following": 120, "follower": 50000, "whisper": 0, "black": 0,
    }
    assert out["stats"] == {
        "archive_view": 12345678, "article_view": 1234, "likes": 654321,
    }
    assert out["overview"] == {"video_count": 77, "article_count": 1, "opus_count": 64}


def test_user_profile_handler_omits_overview_when_missing():
    h = user_profile.HANDLER
    # No overview_stat at all (runner drops optional endpoint when unavailable).
    items = h.extract_items({
        "user_info": _user_info(),
        "relation_info": _relation_info(),
        "up_stat": _up_stat(),
    })
    assert len(items) == 1
    out = h.transform(items[0])
    assert "overview" not in out
    # Required fields still present.
    assert out["uid"] == 13991807
    assert out["stats"]["likes"] == 654321

    # Empty overview_stat dict should also omit (treated as "not available").
    items2 = h.extract_items({
        "user_info": _user_info(),
        "relation_info": _relation_info(),
        "up_stat": _up_stat(),
        "overview_stat": {},
    })
    assert "overview" not in h.transform(items2[0])


def test_user_profile_handler_skips_when_required_missing():
    h = user_profile.HANDLER
    base = {
        "user_info": _user_info(),
        "relation_info": _relation_info(),
        "up_stat": _up_stat(),
    }
    # Each required endpoint absent → empty list.
    for ep in ("user_info", "relation_info", "up_stat"):
        sub = {k: v for k, v in base.items() if k != ep}
        assert h.extract_items(sub) == []
    # Empty raw_payload for a required endpoint → empty list.
    for ep in ("user_info", "relation_info", "up_stat"):
        sub = dict(base)
        sub[ep] = {}
        assert h.extract_items(sub) == []
    # Wholly empty input.
    assert h.extract_items({}) == []


def test_user_profile_handler_tolerates_field_gaps():
    h = user_profile.HANDLER
    # user_info has only mid; everything else missing.
    items = h.extract_items({
        "user_info": {"mid": 42},
        "relation_info": {},  # empty dict counts as missing for required → skipped
        "up_stat": _up_stat(),
    })
    # relation_info empty → skipped (matches "endpoint 缺失或 raw_payload 为空" rule).
    assert items == []

    # Now provide minimal but non-empty payloads for required endpoints.
    items = h.extract_items({
        "user_info": {"mid": 42},
        "relation_info": {"_": 1},  # non-empty but missing typed fields
        "up_stat": {"_": 1},
    })
    assert len(items) == 1
    out = h.transform(items[0])
    assert out["uid"] == 42
    # String fields default to "".
    assert out["name"] == ""
    assert out["sign"] == ""
    assert out["birthday"] == ""
    assert out["avatar"] == ""
    assert out["sex"] == ""
    # Numeric fields default to 0.
    assert out["level"] == 0
    assert out["join_time"] == 0
    # vip degrades to {type:0, status:0} when user_info has no vip block.
    assert out["vip"] == {"type": 0, "status": 0}
    # social / stats default to all-0 dicts.
    assert out["social"] == {"following": 0, "follower": 0, "whisper": 0, "black": 0}
    assert out["stats"] == {"archive_view": 0, "article_view": 0, "likes": 0}
    # overview_stat absent → no key in result.
    assert "overview" not in out
