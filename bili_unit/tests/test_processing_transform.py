# tests for bili_unit/processing — transform handlers (pure functions).

from bili_unit.processing.transform import (
    HANDLERS,
    articles,
    dynamics,
    get_handler,
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

    out1 = h.transform(items[1])
    assert out1["title"] == "Second"
    assert out1["stats"]["view"] == 0


# ---------- registry -----------------------------------------------------

def test_registry_lookup_and_iter():
    assert "video_metadata" in HANDLERS
    assert "dynamics" in HANDLERS
    assert "articles" in HANDLERS
    assert get_handler("video_metadata") is video_metadata.HANDLER
    assert get_handler("dynamics") is dynamics.HANDLER
    assert get_handler("articles") is articles.HANDLER
    assert get_handler("nonexistent") is None
    names = HANDLERS.names()
    assert set(names) >= {"video_metadata", "dynamics", "articles"}
    assert len(HANDLERS) == 3
