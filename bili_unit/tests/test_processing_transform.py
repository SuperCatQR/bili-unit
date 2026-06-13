# tests for bili_unit/processing — transform handlers (pure functions).
#
# After the parsing-layer migration, each handler's transform() receives a
# WorkItem whose item_data is a typed-object dict (the serialised output of
# a parsing model's to_dict()), NOT a raw fetching payload.

from bili_unit.processing.transform import (
    HANDLERS,
    articles,
    dynamics,
    get_handler,
    opus,
    user_profile,
    video_metadata,
)
from bili_unit.processing.transform._base import WorkItem

# ---------- video_metadata ----------------------------------------------

def _vm_item(**overrides):
    """Build a WorkItem carrying a VideoDetail typed-object dict."""
    d: dict = {
        "bvid": "BV1xx",
        "aid": 12345,
        "title": "Hello",
        "desc": "long description",
        "duration": 600,
        "ctime": 1700000000,
        "pubdate": 1700000000,
        "pic": "https://pic",
        "pages": [
            {"cid": 11, "part": "P1", "duration": 300, "dimension": {"w": 1920}, "first_frame": ""},
            {"cid": 12, "part": "P2", "duration": 300, "dimension": {"w": 1920}, "first_frame": ""},
        ],
        "tags": ["hello", "world", "string-tag"],
        "stat": {"view": 10000, "danmaku": 50, "reply": 5, "favorite": 100, "coin": 30, "share": 12, "like": 999},
        "owner": {"mid": 7, "name": "U", "face": "https://x"},
        "rights": {"download": 0},
        "subtitle": {"list": []},
        "label": {"name": "test"},
        "pic_local": "",
    }
    d.update(overrides)
    return WorkItem(item_type="video_metadata", item_id=d["bvid"], item_data=d)


def test_video_metadata_handler_transform():
    out = video_metadata.HANDLER.transform(_vm_item())
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
    out = video_metadata.HANDLER.transform(_vm_item(
        bvid="BV2", aid=None, title="", desc="", duration=0,
        ctime=None, pubdate=None, pages=[], tags=[],
        stat={}, owner={}, rights={}, subtitle={}, label={},
    ))
    assert out["bvid"] == "BV2"
    assert out["aid"] is None
    assert out["title"] == ""
    assert out["pages"] == []
    assert out["tags"] == []
    assert out["stat"] == {}
    assert out["owner"] == {}


def test_video_metadata_handler_pages_drop_first_frame():
    out = video_metadata.HANDLER.transform(_vm_item())
    # first_frame is present in the typed-object dict but dropped from result.
    for p in out["pages"]:
        assert "first_frame" not in p


# ---------- dynamics ------------------------------------------------------

def _dyn_item(**overrides):
    """Build a WorkItem carrying a DynamicPost typed-object dict."""
    d: dict = {
        "id_str": "100",
        "type": "DYNAMIC_TYPE_WORD",
        "text": "plain word",
        "timestamp": 1718000000,
        "major": {},
        "forwarded": None,
        "image_urls": [],
        "image_locals": [],
    }
    d.update(overrides)
    return WorkItem(item_type="dynamics", item_id=d["id_str"], item_data=d)


def test_dynamics_handler_word_type():
    out = dynamics.HANDLER.transform(_dyn_item())
    assert out["id_str"] == "100"
    assert out["type"] == "DYNAMIC_TYPE_WORD"
    assert out["text"] == "plain word"
    assert out["timestamp"] == 1718000000
    assert out["major"] == {}
    assert out["forwarded"] is None


def test_dynamics_handler_draw_type_extracts_images():
    out = dynamics.HANDLER.transform(_dyn_item(
        id_str="200",
        type="DYNAMIC_TYPE_DRAW",
        text="图文动态",
        major={"type": "MAJOR_TYPE_DRAW", "images": ["https://img/1.jpg", "https://img/2.jpg"]},
    ))
    assert out["text"] == "图文动态"
    assert out["major"]["type"] == "MAJOR_TYPE_DRAW"
    assert out["major"]["images"] == ["https://img/1.jpg", "https://img/2.jpg"]


def test_dynamics_handler_archive_type():
    out = dynamics.HANDLER.transform(_dyn_item(
        id_str="300",
        type="DYNAMIC_TYPE_AV",
        text="",
        major={
            "type": "MAJOR_TYPE_ARCHIVE",
            "bvid": "BV1xx", "aid": "12345",
            "title": "Video Title", "desc": "video desc",
            "duration_text": "10:00",
            "jump_url": "//www.bilibili.com/video/BV1xx",
            "cover": "https://cover/x.jpg",
        },
    ))
    assert out["text"] == ""
    assert out["major"]["bvid"] == "BV1xx"
    assert out["major"]["title"] == "Video Title"


def test_dynamics_handler_forward_type_includes_orig():
    out = dynamics.HANDLER.transform(_dyn_item(
        id_str="400",
        type="DYNAMIC_TYPE_FORWARD",
        text="转发评论",
        major={},
        forwarded={
            "id_str": "300",
            "type": "DYNAMIC_TYPE_AV",
            "text": "",
            "timestamp": 1718000002,
            "major": {
                "type": "MAJOR_TYPE_ARCHIVE",
                "bvid": "BV1xx", "title": "orig title",
            },
        },
    ))
    assert out["text"] == "转发评论"
    assert out["forwarded"] is not None
    assert out["forwarded"]["id_str"] == "300"
    assert out["forwarded"]["major"]["bvid"] == "BV1xx"
    assert out["forwarded"]["major"]["title"] == "orig title"


def test_dynamics_handler_minimal_dict():
    """Bare-minimum typed-object dict still produces a valid result."""
    out = dynamics.HANDLER.transform(WorkItem(
        item_type="dynamics", item_id="999",
        item_data={"id_str": "999"},
    ))
    assert out["id_str"] == "999"
    assert out["text"] == ""
    assert out["timestamp"] is None
    assert out["forwarded"] is None


# ---------- articles ------------------------------------------------------

def _art_item(**overrides):
    """Build a WorkItem carrying an Article typed-object dict."""
    d: dict = {
        "id": "555",
        "title": "Hello",
        "summary": "summary text",
        "image_urls": ["https://a.jpg", "https://b.jpg"],
        "stats": {"view": 10, "favorite": 0, "like": 2, "reply": 0, "share": 0, "coin": 0},
        "ctime": 1700000000,
        "lists": [],
        "markdown": "",
        "content_json": [],
        "image_locals": [],
    }
    d.update(overrides)
    return WorkItem(item_type="articles", item_id=d["id"], item_data=d)


def test_articles_handler_transform():
    out = articles.HANDLER.transform(_art_item())
    assert out["id"] == "555"
    assert out["title"] == "Hello"
    assert out["image_urls"] == ["https://a.jpg", "https://b.jpg"]
    assert out["stats"]["view"] == 10
    assert out["stats"]["like"] == 2
    assert out["stats"]["reply"] == 0
    assert out["ctime"] == 1700000000
    assert out["markdown"] == ""
    assert out["content_json"] == []
    assert out["word_count"] == 0


def test_articles_handler_second_item_defaults():
    out = articles.HANDLER.transform(_art_item(
        id="666", title="Second", summary="",
        image_urls=[], stats={}, ctime=None,
    ))
    assert out["title"] == "Second"
    assert out["stats"] == {}
    assert out["image_urls"] == []


def test_articles_handler_attaches_readlist_membership():
    out = articles.HANDLER.transform(_art_item(
        id="100", title="in-readlist",
        lists=[{"rlid": "1043430", "name": "【警戒追踪】"}],
    ))
    assert out["lists"] == [{"rlid": "1043430", "name": "【警戒追踪】"}]


def test_articles_handler_two_readlists():
    out = articles.HANDLER.transform(_art_item(
        id="200", title="in-two-readlists",
        lists=[
            {"rlid": "1043430", "name": "【警戒追踪】"},
            {"rlid": "1043431", "name": "【前哨速递】"},
        ],
    ))
    rlids = {m["rlid"] for m in out["lists"]}
    assert rlids == {"1043430", "1043431"}


def test_articles_handler_lists_defaults_empty_when_absent():
    out = articles.HANDLER.transform(_art_item(id="1", title="t"))
    assert out["lists"] == []


def test_articles_handler_attaches_article_detail():
    out = articles.HANDLER.transform(_art_item(
        id="100", title="with-body",
        markdown="正文一段话。",
        content_json=[{"type": "ParagraphNode", "text": "正文一段话。"}],
    ))
    assert out["markdown"] == "正文一段话。"
    assert out["content_json"] == [{"type": "ParagraphNode", "text": "正文一段话。"}]
    assert out["word_count"] == len("正文一段话。")


def test_articles_handler_no_detail_fallback():
    out = articles.HANDLER.transform(_art_item(id="200", title="no-detail"))
    assert out["markdown"] == ""
    assert out["content_json"] == []
    assert out["word_count"] == 0


# ---------- opus ---------------------------------------------------------

def _opus_item(**overrides):
    """Build a WorkItem carrying an OpusPost typed-object dict."""
    d: dict = {
        "id": "100",
        "title": "图文一",
        "summary": "summary text",
        "cover": "https://i0.hdslb.com/c.jpg",
        "jump_url": "//opus.bilibili.com/100",
        "stats": {"view": 10, "favorite": 0, "like": 2, "reply": 0, "share": 0, "coin": 0},
        "ctime": 1700000000,
        "list_images": [],
        "markdown": "",
        "detail_images": [],
        "cover_local": "",
        "image_locals": [],
    }
    d.update(overrides)
    return WorkItem(item_type="opus", item_id=d["id"], item_data=d)


def test_opus_handler_transform_list_only():
    out = opus.HANDLER.transform(_opus_item())
    assert out["id"] == "100"
    assert out["title"] == "图文一"
    assert out["summary"] == "summary text"
    # Cover is the only image when no detail_images or list_images.
    assert out["image_urls"] == ["https://i0.hdslb.com/c.jpg"]
    assert out["stats"]["view"] == 10
    assert out["stats"]["like"] == 2
    assert out["ctime"] == 1700000000
    assert out["jump_url"] == "//opus.bilibili.com/100"
    assert out["markdown"] == ""
    assert out["images"] == []
    assert out["word_count"] == 0


def test_opus_handler_no_cover_no_pics():
    out = opus.HANDLER.transform(_opus_item(
        id="200", title="图文二", summary="",
        cover="", stats={},
    ))
    assert out["title"] == "图文二"
    assert out["stats"] == {}
    assert out["image_urls"] == []


def test_opus_handler_attaches_opus_detail():
    out = opus.HANDLER.transform(_opus_item(
        id="300", title="with-body",
        markdown="正文一段话。",
        detail_images=[{"url": "https://i0.hdslb.com/x.jpg", "width": 100, "height": 80}],
    ))
    assert out["markdown"] == "正文一段话。"
    assert out["images"] == [{"url": "https://i0.hdslb.com/x.jpg", "width": 100, "height": 80}]
    assert out["image_urls"] == ["https://i0.hdslb.com/x.jpg"]
    assert out["word_count"] == len("正文一段话。")


def test_opus_handler_detail_fallback():
    out = opus.HANDLER.transform(_opus_item(id="400", title="no-detail"))
    assert out["markdown"] == ""
    assert out["images"] == []
    assert out["word_count"] == 0


def test_opus_handler_list_images_fallback():
    """No detail_images → image_urls falls back to list_images."""
    out = opus.HANDLER.transform(_opus_item(
        id="500", title="list-pics",
        cover="",
        list_images=["https://i0.hdslb.com/p1.jpg", "https://i0.hdslb.com/p2.jpg"],
    ))
    assert out["image_urls"] == ["https://i0.hdslb.com/p1.jpg", "https://i0.hdslb.com/p2.jpg"]


def test_opus_handler_cover_dedup():
    """Cover is prepended only when not already in image_urls."""
    out = opus.HANDLER.transform(_opus_item(
        id="600", cover="https://i0.hdslb.com/c.jpg",
        detail_images=[{"url": "https://i0.hdslb.com/c.jpg", "width": 1, "height": 1}],
    ))
    # Cover already in detail_images → not duplicated.
    assert out["image_urls"] == ["https://i0.hdslb.com/c.jpg"]


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

def _up_item(**overrides):
    """Build a WorkItem carrying an UpProfile typed-object dict."""
    d: dict = {
        "mid": 13991807,
        "name": "测试 UP",
        "sex": "男",
        "sign": "测试签名",
        "avatar": "https://i0.hdslb.com/u.jpg",
        "birthday": "06-11",
        "level": 6,
        "jointime": 1500000000,
        "vip": {"type": 1, "status": 1, "label": "年度大会员"},
        "social": {"following": 120, "follower": 50000, "whisper": 0, "black": 0},
        "stats": {"archive_view": 12345678, "article_view": 1234, "likes": 654321},
        "overview": {"video_count": 77, "article_count": 1, "opus_count": 64},
        "avatar_local": "",
    }
    d.update(overrides)
    return WorkItem(item_type="user_profile", item_id=str(d["mid"]), item_data=d)


def test_user_profile_handler_full_happy_path():
    out = user_profile.HANDLER.transform(_up_item())
    assert out["uid"] == 13991807
    assert out["name"] == "测试 UP"
    assert out["sex"] == "男"
    assert out["sign"] == "测试签名"
    assert out["avatar"] == "https://i0.hdslb.com/u.jpg"
    assert out["birthday"] == "06-11"
    assert out["level"] == 6
    assert out["vip"] == {"type": 1, "status": 1, "label": "年度大会员"}
    assert out["join_time"] == 1500000000
    assert out["social"] == {"following": 120, "follower": 50000, "whisper": 0, "black": 0}
    assert out["stats"] == {"archive_view": 12345678, "article_view": 1234, "likes": 654321}
    assert out["overview"] == {"video_count": 77, "article_count": 1, "opus_count": 64}


def test_user_profile_handler_omits_overview_when_missing():
    out = user_profile.HANDLER.transform(_up_item(overview=None))
    assert "overview" not in out
    assert out["uid"] == 13991807
    assert out["stats"]["likes"] == 654321


def test_user_profile_handler_omits_empty_overview():
    out = user_profile.HANDLER.transform(_up_item(overview={}))
    assert "overview" not in out


def test_user_profile_handler_tolerates_field_gaps():
    out = user_profile.HANDLER.transform(_up_item(
        mid=42, name="", sex="", sign="", avatar="", birthday="",
        level=0, jointime=0,
        vip={"type": 0, "status": 0, "label": ""},
        social={"following": 0, "follower": 0, "whisper": 0, "black": 0},
        stats={"archive_view": 0, "article_view": 0, "likes": 0},
        overview=None,
    ))
    assert out["uid"] == 42
    assert out["name"] == ""
    assert out["sign"] == ""
    assert out["birthday"] == ""
    assert out["avatar"] == ""
    assert out["sex"] == ""
    assert out["level"] == 0
    assert out["join_time"] == 0
    assert out["vip"] == {"type": 0, "status": 0, "label": ""}
    assert out["social"] == {"following": 0, "follower": 0, "whisper": 0, "black": 0}
    assert out["stats"] == {"archive_view": 0, "article_view": 0, "likes": 0}
    assert "overview" not in out


def test_user_profile_handler_uid_fallback():
    """When mid is None in the dict, uid falls back to int(item_id)."""
    item = WorkItem(
        item_type="user_profile", item_id="42",
        item_data={"mid": None, "name": "X"},
    )
    out = user_profile.HANDLER.transform(item)
    assert out["uid"] == 42
