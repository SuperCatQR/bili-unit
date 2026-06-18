from __future__ import annotations

import pytest

from bili_unit.parsing._images import ImageDownloadResult
from bili_unit.parsing.models.article import Article
from bili_unit.parsing.models.dynamic import DynamicPost
from bili_unit.parsing.models.opus import OpusPost
from bili_unit.parsing.models.up_profile import UpProfile
from bili_unit.parsing.models.video_detail import VideoDetail

# ---------------------------------------------------------------------------
# Test data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def up_profile_data():
    user_info = {
        "mid": 12345,
        "name": "test_up",
        "sex": "男",
        "sign": "hello",
        "face": "https://example.com/face.jpg",
        "birthday": "01-01",
        "level": 5,
        "jointime": 1500000000,
        "vip": {"type": 2, "status": 1, "label": {"text": "年度大会员"}},
    }
    relation_info = {"following": 100, "follower": 50000, "whisper": 10, "black": 5}
    up_stat = {
        "archive": {"view": 1000000},
        "article": {"view": 50000},
        "likes": 80000,
    }
    overview_stat = {"video": 200, "article": 50, "opus": 30}
    return user_info, relation_info, up_stat, overview_stat


@pytest.fixture
def video_detail_data():
    return {
        "info": {
            "bvid": "BV1xx411c7mD",
            "aid": 12345,
            "title": "Test Video",
            "desc": "desc",
            "duration": 300,
            "ctime": 1500000000,
            "pubdate": 1500000001,
            "pic": "https://example.com/cover.jpg",
            "pages": [
                {
                    "cid": 111,
                    "part": "Part 1",
                    "duration": 300,
                    "dimension": {"width": 1920, "height": 1080},
                    "first_frame": "",
                }
            ],
            "stat": {
                "view": 10000,
                "danmaku": 500,
                "reply": 200,
                "favorite": 300,
                "coin": 100,
                "share": 50,
                "like": 800,
            },
            "owner": {
                "mid": 12345,
                "name": "test_up",
                "face": "https://example.com/face.jpg",
            },
            "rights": {"bp": 0},
            "subtitle": {},
            "label": {},
        },
        "tags": [{"tag_name": "tag1"}, {"tag_name": "tag2"}],
    }


@pytest.fixture
def article_data():
    list_item = {
        "id": 12345678,
        "title": "Test Article",
        "summary": "A summary",
        "image_urls": [
            "https://example.com/img1.jpg",
            "https://example.com/img2.jpg",
        ],
        "banner_url": "https://example.com/banner.jpg",
        "stats": {
            "view": 1000,
            "favorite": 50,
            "like": 100,
            "reply": 20,
            "share": 10,
            "coin": 30,
        },
        "ctime": 1600000000,
    }
    detail = {
        "markdown": "# Hello",
        "content_json": [{"type": "paragraph", "content": "text"}],
    }
    list_membership = [{"rlid": "100", "name": "MyReadList"}]
    return list_item, detail, list_membership


@pytest.fixture
def opus_data():
    list_item = {
        "opus_id": "opus_001",
        "title": "My Opus",
        "summary": "Opus summary",
        "cover": "https://example.com/cover.jpg",
        "jump_url": "https://bilibili.com/opus/1",
        "stats": {
            "view": 500,
            "favorite": 10,
            "like": 50,
            "reply": 5,
            "share": 2,
            "coin": 8,
        },
        "pub_time": 1700000000,
        "modules": {},
    }
    detail = {
        "markdown": "# Opus Content",
        "images": [
            {"url": "https://example.com/img1.jpg"},
            {"url": "https://example.com/img2.jpg"},
        ],
    }
    return list_item, detail


@pytest.fixture
def dynamic_data():
    raw = {
        "id_str": "dyn_001",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": 1700000000},
            "module_dynamic": {
                "desc": {"text": "Some text"},
                "major": {
                    "type": "MAJOR_TYPE_DRAW",
                    "draw": {
                        "items": [
                            {"src": "https://example.com/dyn1.jpg"},
                            {"src": "https://example.com/dyn2.jpg"},
                        ]
                    },
                },
            },
        },
    }
    return raw


@pytest.fixture
def dynamic_forward_data():
    raw_forward = {
        "id_str": "dyn_002",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"pub_ts": 1700001000},
            "module_dynamic": {"desc": {"text": "Forwarded!"}},
        },
        "orig": {
            "id_str": "dyn_orig",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"pub_ts": 1699999000},
                "module_dynamic": {
                    "desc": {"text": "Original text"},
                    "major": {
                        "type": "MAJOR_TYPE_DRAW",
                        "draw": {"items": [{"src": "https://example.com/orig.jpg"}]},
                    },
                },
            },
        },
    }
    return raw_forward


# ---------------------------------------------------------------------------
# UpProfile tests
# ---------------------------------------------------------------------------

class TestUpProfile:
    def test_from_raw(self, up_profile_data):
        user_info, relation_info, up_stat, overview_stat = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat, overview_stat)

        assert profile.mid == 12345
        assert profile.name == "test_up"
        assert profile.sex == "男"
        assert profile.sign == "hello"
        assert profile.face_url == "https://example.com/face.jpg"
        assert profile.birthday == "01-01"
        assert profile.level == 5
        assert profile.jointime == 1500000000
        assert profile.vip["type"] == 2
        assert profile.vip["status"] == 1
        assert profile.vip["label"] == "年度大会员"
        assert profile.social["following"] == 100
        assert profile.social["follower"] == 50000
        assert profile.stats["archive_view"] == 1000000
        assert profile.stats["article_view"] == 50000
        assert profile.stats["likes"] == 80000
        assert profile.overview["video_count"] == 200
        assert profile.overview["article_count"] == 50
        assert profile.overview["opus_count"] == 30

    def test_from_raw_without_overview(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat, None)

        assert profile.mid == 12345
        assert profile.overview is None

    def test_item_id(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        assert profile.item_id == "12345"

    def test_to_dict_from_dict_roundtrip(self, up_profile_data):
        user_info, relation_info, up_stat, overview_stat = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat, overview_stat)

        d = profile.to_dict()
        restored = UpProfile.from_dict(d)

        assert restored.mid == profile.mid
        assert restored.name == profile.name
        assert restored.sex == profile.sex
        assert restored.sign == profile.sign
        assert restored.face_url == profile.face_url
        assert restored.birthday == profile.birthday
        assert restored.level == profile.level
        assert restored.jointime == profile.jointime
        assert restored.vip == profile.vip
        assert restored.social == profile.social
        assert restored.stats == profile.stats
        assert restored.overview == profile.overview
        assert restored.avatar_local == profile.avatar_local

    def test_collect_image_jobs_with_avatar(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        jobs = profile.collect_image_jobs(uid=12345)

        assert len(jobs) == 1
        assert jobs[0] == ("https://example.com/face.jpg", "avatar.jpg")

    def test_collect_image_jobs_without_avatar(self):
        profile = UpProfile(mid=999, name="no_avatar")

        jobs = profile.collect_image_jobs(uid=999)

        assert jobs == []

    def test_apply_image_results_ok(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        result = ImageDownloadResult(
            url="https://example.com/face.jpg",
            local_path="avatar.jpg",
            status="ok",
        )
        profile.apply_image_results([result])

        assert profile.avatar_local == "avatar.jpg"

    def test_apply_image_results_skipped(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        result = ImageDownloadResult(
            url="https://example.com/face.jpg",
            local_path="avatar.jpg",
            status="skipped",
        )
        profile.apply_image_results([result])

        assert profile.avatar_local == "avatar.jpg"

    def test_apply_image_results_failed(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        result = ImageDownloadResult(
            url="https://example.com/face.jpg",
            local_path="avatar.jpg",
            status="failed",
            error="HTTP 404",
        )
        profile.apply_image_results([result])

        assert profile.avatar_local == ""

    def test_apply_image_results_empty(self, up_profile_data):
        user_info, relation_info, up_stat, _ = up_profile_data
        profile = UpProfile.from_raw(user_info, relation_info, up_stat)

        profile.apply_image_results([])

        assert profile.avatar_local == ""


# ---------------------------------------------------------------------------
# VideoDetail tests
# ---------------------------------------------------------------------------

class TestVideoDetail:
    def test_from_raw(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        assert video.bvid == "BV1xx411c7mD"
        assert video.aid == 12345
        assert video.title == "Test Video"
        assert video.description == "desc"
        assert video.duration_s == 300
        assert video.ctime == 1500000000
        assert video.pubdate_ms == 1500000001 * 1000
        assert video.cover_url == "https://example.com/cover.jpg"
        assert len(video.pages) == 1
        assert video.pages[0].cid == 111
        assert video.pages[0].part == "Part 1"
        assert video.pages[0].duration == 300  # PageInfo.duration unchanged
        assert video.pages[0].dimension == {"width": 1920, "height": 1080}
        assert video.tags == ["tag1", "tag2"]
        assert video.stat.view == 10000
        assert video.stat.danmaku == 500
        assert video.stat.reply == 200
        assert video.stat.favorite == 300
        assert video.stat.coin == 100
        assert video.stat.share == 50
        assert video.stat.like == 800
        assert video.owner.mid == 12345
        assert video.owner.name == "test_up"
        assert video.owner.face == "https://example.com/face.jpg"

    def test_item_id(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        assert video.item_id == "BV1xx411c7mD"

    def test_to_dict_from_dict_roundtrip(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        d = video.to_dict()
        restored = VideoDetail.from_dict(d)

        assert restored.bvid == video.bvid
        assert restored.aid == video.aid
        assert restored.title == video.title
        assert restored.description == video.description
        assert restored.duration_s == video.duration_s
        assert restored.ctime == video.ctime
        assert restored.pubdate_ms == video.pubdate_ms
        assert restored.cover_url == video.cover_url
        assert len(restored.pages) == len(video.pages)
        assert restored.pages[0].cid == video.pages[0].cid
        assert restored.pages[0].part == video.pages[0].part
        assert restored.tags == video.tags
        assert restored.stat.view == video.stat.view
        assert restored.stat.danmaku == video.stat.danmaku
        assert restored.owner.mid == video.owner.mid
        assert restored.owner.name == video.owner.name
        assert restored.pic_local == video.pic_local

    def test_collect_image_jobs_with_pic(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        jobs = video.collect_image_jobs(uid=12345)

        assert len(jobs) == 1
        assert jobs[0] == (
            "https://example.com/cover.jpg",
            "video/BV1xx411c7mD_cover.jpg",
        )

    def test_collect_image_jobs_without_pic(self):
        video = VideoDetail(bvid="BV1test", title="No Cover")

        jobs = video.collect_image_jobs(uid=12345)

        assert jobs == []

    def test_apply_image_results_ok(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        result = ImageDownloadResult(
            url="https://example.com/cover.jpg",
            local_path="video/BV1xx411c7mD_cover.jpg",
            status="ok",
        )
        video.apply_image_results([result])

        assert video.pic_local == "video/BV1xx411c7mD_cover.jpg"

    def test_apply_image_results_failed(self, video_detail_data):
        video = VideoDetail.from_raw(video_detail_data)

        result = ImageDownloadResult(
            url="https://example.com/cover.jpg",
            local_path="video/BV1xx411c7mD_cover.jpg",
            status="failed",
            error="timeout",
        )
        video.apply_image_results([result])

        assert video.pic_local == ""

    def test_from_raw_empty_payload(self):
        video = VideoDetail.from_raw({})

        assert video.bvid == ""
        assert video.aid is None
        assert video.title == ""
        assert video.pages == []
        assert video.tags == []

    def test_from_raw_missing_optional_fields(self):
        raw = {
            "info": {
                "bvid": "BV1minimal",
                "title": "Minimal Video",
            },
            "tags": [],
        }
        video = VideoDetail.from_raw(raw)

        assert video.bvid == "BV1minimal"
        assert video.aid is None
        assert video.duration_s == 0
        assert video.pages == []
        assert video.stat.view == 0
        assert video.owner.mid is None


# ---------------------------------------------------------------------------
# Article tests
# ---------------------------------------------------------------------------

class TestArticle:
    def test_from_raw(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        assert article.id == "12345678"
        assert article.title == "Test Article"
        assert article.summary == "A summary"
        assert "https://example.com/img1.jpg" in article.image_urls
        assert "https://example.com/img2.jpg" in article.image_urls
        assert "https://example.com/banner.jpg" in article.image_urls
        assert article.stats.view == 1000
        assert article.stats.favorite == 50
        assert article.stats.like == 100
        assert article.ctime == 1600000000
        assert len(article.lists) == 1
        assert article.lists[0].rlid == "100"
        assert article.lists[0].name == "MyReadList"
        assert article.markdown == "# Hello"
        assert article.content_json == [{"type": "paragraph", "content": "text"}]

    def test_item_id(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        assert article.item_id == "12345678"

    def test_to_dict_from_dict_roundtrip(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        d = article.to_dict()
        restored = Article.from_dict(d)

        assert restored.id == article.id
        assert restored.title == article.title
        assert restored.summary == article.summary
        assert restored.image_urls == article.image_urls
        assert restored.stats.view == article.stats.view
        assert restored.stats.favorite == article.stats.favorite
        assert restored.ctime == article.ctime
        assert len(restored.lists) == len(article.lists)
        assert restored.lists[0].rlid == article.lists[0].rlid
        assert restored.markdown == article.markdown
        assert restored.content_json == article.content_json
        assert restored.image_locals == article.image_locals

    def test_collect_image_jobs(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        jobs = article.collect_image_jobs(uid=12345)

        assert len(jobs) == 3
        assert jobs[0] == ("https://example.com/img1.jpg", "article/12345678_00.jpg")
        assert jobs[1] == ("https://example.com/img2.jpg", "article/12345678_01.jpg")
        assert jobs[2] == ("https://example.com/banner.jpg", "article/12345678_02.jpg")

    def test_collect_image_jobs_empty(self):
        article = Article(id="999", title="No Images")

        jobs = article.collect_image_jobs(uid=12345)

        assert jobs == []

    def test_apply_image_results(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        results = [
            ImageDownloadResult(url="img1", local_path="article/12345678_00.jpg", status="ok"),
            ImageDownloadResult(url="img2", local_path="article/12345678_01.jpg", status="skipped"),
            ImageDownloadResult(url="banner", local_path="article/12345678_02.jpg", status="ok"),
        ]
        article.apply_image_results(results)

        assert len(article.image_locals) == 3
        assert "article/12345678_00.jpg" in article.image_locals
        assert "article/12345678_01.jpg" in article.image_locals
        assert "article/12345678_02.jpg" in article.image_locals

    def test_apply_image_results_with_failures(self, article_data):
        list_item, detail, list_membership = article_data
        article = Article.from_raw(list_item, detail, list_membership)

        results = [
            ImageDownloadResult(url="img1", local_path="article/12345678_00.jpg", status="ok"),
            ImageDownloadResult(url="img2", local_path="article/12345678_01.jpg", status="failed"),
            ImageDownloadResult(url="banner", local_path="article/12345678_02.jpg", status="ok"),
        ]
        article.apply_image_results(results)

        assert len(article.image_locals) == 2
        assert "article/12345678_00.jpg" in article.image_locals
        assert "article/12345678_02.jpg" in article.image_locals

    def test_from_raw_without_detail(self, article_data):
        list_item, _, list_membership = article_data
        article = Article.from_raw(list_item, None, list_membership)

        assert article.id == "12345678"
        assert article.markdown == ""
        assert article.content_json == []

    def test_from_raw_empty_lists(self):
        list_item = {
            "id": 999,
            "title": "Empty",
            "summary": "",
            "image_urls": [],
            "stats": {},
            "ctime": None,
        }
        article = Article.from_raw(list_item, None, [])

        assert article.id == "999"
        assert article.lists == []
        assert article.image_urls == []


# ---------------------------------------------------------------------------
# OpusPost tests
# ---------------------------------------------------------------------------

class TestOpusPost:
    def test_from_raw(self, opus_data):
        list_item, detail = opus_data
        opus = OpusPost.from_raw(list_item, detail)

        assert opus.id == "opus_001"
        assert opus.title == "My Opus"
        assert opus.summary == "Opus summary"
        assert opus.cover == "https://example.com/cover.jpg"
        assert opus.jump_url == "https://bilibili.com/opus/1"
        assert opus.stats.view == 500
        assert opus.stats.favorite == 10
        assert opus.stats.like == 50
        assert opus.ctime == 1700000000
        assert opus.markdown == "# Opus Content"
        assert len(opus.images) == 2
        assert opus.images[0]["url"] == "https://example.com/img1.jpg"
        assert opus.images[1]["url"] == "https://example.com/img2.jpg"

    def test_item_id(self, opus_data):
        list_item, detail = opus_data
        opus = OpusPost.from_raw(list_item, detail)

        assert opus.item_id == "opus_001"

    def test_to_dict_from_dict_roundtrip(self, opus_data):
        list_item, detail = opus_data
        opus = OpusPost.from_raw(list_item, detail)

        d = opus.to_dict()
        restored = OpusPost.from_dict(d)

        assert restored.id == opus.id
        assert restored.title == opus.title
        assert restored.summary == opus.summary
        assert restored.cover == opus.cover
        assert restored.jump_url == opus.jump_url
        assert restored.stats.view == opus.stats.view
        assert restored.ctime == opus.ctime
        assert restored.markdown == opus.markdown
        assert len(restored.images) == len(opus.images)
        assert [img["url"] for img in restored.images] == [
            img["url"] for img in opus.images
        ]
        assert restored.cover_local == opus.cover_local

    def test_collect_image_jobs_with_cover_and_detail(self, opus_data):
        list_item, detail = opus_data
        opus = OpusPost.from_raw(list_item, detail)

        jobs = opus.collect_image_jobs(uid=12345)

        assert len(jobs) == 3
        assert jobs[0] == ("https://example.com/cover.jpg", "opus/opus_001_cover.jpg")
        assert jobs[1] == ("https://example.com/img1.jpg", "opus/opus_001_00.jpg")
        assert jobs[2] == ("https://example.com/img2.jpg", "opus/opus_001_01.jpg")

    def test_collect_image_jobs_without_cover(self):
        opus = OpusPost(
            id="opus_no_cover",
            images=[{"url": "https://example.com/img1.jpg"}],
        )

        jobs = opus.collect_image_jobs(uid=12345)

        assert len(jobs) == 1
        assert jobs[0] == ("https://example.com/img1.jpg", "opus/opus_no_cover_00.jpg")

    def test_collect_image_jobs_empty(self):
        opus = OpusPost(id="opus_empty")

        jobs = opus.collect_image_jobs(uid=12345)

        assert jobs == []

    def test_apply_image_results_with_cover(self, opus_data):
        list_item, detail = opus_data
        opus = OpusPost.from_raw(list_item, detail)

        results = [
            ImageDownloadResult(
                url="https://example.com/cover.jpg",
                local_path="opus/opus_001_cover.jpg",
                status="ok",
            ),
            ImageDownloadResult(
                url="https://example.com/img1.jpg",
                local_path="opus/opus_001_00.jpg",
                status="ok",
            ),
            ImageDownloadResult(
                url="https://example.com/img2.jpg",
                local_path="opus/opus_001_01.jpg",
                status="skipped",
            ),
        ]
        opus.apply_image_results(results)

        assert opus.cover_local == "opus/opus_001_cover.jpg"
        locals_ = [img.get("local_path", "") for img in opus.images]
        assert "opus/opus_001_00.jpg" in locals_
        assert "opus/opus_001_01.jpg" in locals_

    def test_apply_image_results_without_cover(self):
        opus = OpusPost(
            id="opus_no_cover",
            images=[{"url": "https://example.com/img1.jpg"}],
        )

        results = [
            ImageDownloadResult(
                url="https://example.com/img1.jpg",
                local_path="opus/opus_no_cover_00.jpg",
                status="ok",
            ),
        ]
        opus.apply_image_results(results)

        assert opus.cover_local == ""
        assert opus.images[0]["local_path"] == "opus/opus_no_cover_00.jpg"

    def test_from_raw_without_detail(self, opus_data):
        list_item, _ = opus_data
        opus = OpusPost.from_raw(list_item, None)

        assert opus.id == "opus_001"
        assert opus.markdown == ""
        assert opus.images == []


# ---------------------------------------------------------------------------
# DynamicPost tests
# ---------------------------------------------------------------------------

class TestDynamicPost:
    def test_from_raw_draw(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        assert dynamic.id_str == "dyn_001"
        assert dynamic.type == "DYNAMIC_TYPE_DRAW"
        assert dynamic.text == "Some text"
        assert dynamic.timestamp == 1700000000
        assert dynamic.major["type"] == "MAJOR_TYPE_DRAW"
        assert dynamic.forwarded is None
        assert len(dynamic.image_urls) == 2
        assert "https://example.com/dyn1.jpg" in dynamic.image_urls
        assert "https://example.com/dyn2.jpg" in dynamic.image_urls

    def test_from_raw_forward(self, dynamic_forward_data):
        dynamic = DynamicPost.from_raw(dynamic_forward_data)

        assert dynamic.id_str == "dyn_002"
        assert dynamic.type == "DYNAMIC_TYPE_FORWARD"
        assert dynamic.text == "Forwarded!"
        assert dynamic.timestamp == 1700001000
        assert dynamic.forwarded is not None
        assert dynamic.forwarded.id_str == "dyn_orig"
        assert dynamic.forwarded.type == "DYNAMIC_TYPE_DRAW"
        assert dynamic.forwarded.text == "Original text"
        assert dynamic.forwarded.timestamp == 1699999000
        assert dynamic.forwarded.major["type"] == "MAJOR_TYPE_DRAW"
        assert len(dynamic.image_urls) == 1
        assert "https://example.com/orig.jpg" in dynamic.image_urls

    def test_item_id(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        assert dynamic.item_id == "dyn_001"

    def test_to_dict_from_dict_roundtrip(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        d = dynamic.to_dict()
        restored = DynamicPost.from_dict(d)

        assert restored.id_str == dynamic.id_str
        assert restored.type == dynamic.type
        assert restored.text == dynamic.text
        assert restored.timestamp == dynamic.timestamp
        assert restored.major == dynamic.major
        assert restored.forwarded is None
        assert restored.image_urls == dynamic.image_urls
        assert restored.image_locals == dynamic.image_locals

    def test_to_dict_from_dict_roundtrip_with_forward(self, dynamic_forward_data):
        dynamic = DynamicPost.from_raw(dynamic_forward_data)

        d = dynamic.to_dict()
        restored = DynamicPost.from_dict(d)

        assert restored.id_str == dynamic.id_str
        assert restored.forwarded is not None
        assert restored.forwarded.id_str == dynamic.forwarded.id_str
        assert restored.forwarded.type == dynamic.forwarded.type
        assert restored.forwarded.text == dynamic.forwarded.text
        assert restored.forwarded.timestamp == dynamic.forwarded.timestamp
        assert restored.image_urls == dynamic.image_urls

    def test_collect_image_jobs(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        jobs = dynamic.collect_image_jobs(uid=12345)

        assert len(jobs) == 2
        assert jobs[0] == ("https://example.com/dyn1.jpg", "dynamic/dyn_001_00.jpg")
        assert jobs[1] == ("https://example.com/dyn2.jpg", "dynamic/dyn_001_01.jpg")

    def test_collect_image_jobs_forward(self, dynamic_forward_data):
        dynamic = DynamicPost.from_raw(dynamic_forward_data)

        jobs = dynamic.collect_image_jobs(uid=12345)

        assert len(jobs) == 1
        assert jobs[0] == ("https://example.com/orig.jpg", "dynamic/dyn_002_00.jpg")

    def test_collect_image_jobs_empty(self):
        dynamic = DynamicPost(id_str="dyn_empty", type="DYNAMIC_TYPE_TEXT")

        jobs = dynamic.collect_image_jobs(uid=12345)

        assert jobs == []

    def test_apply_image_results(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        results = [
            ImageDownloadResult(url="dyn1", local_path="dynamic/dyn_001_00.jpg", status="ok"),
            ImageDownloadResult(url="dyn2", local_path="dynamic/dyn_001_01.jpg", status="skipped"),
        ]
        dynamic.apply_image_results(results)

        assert len(dynamic.image_locals) == 2
        assert "dynamic/dyn_001_00.jpg" in dynamic.image_locals
        assert "dynamic/dyn_001_01.jpg" in dynamic.image_locals

    def test_apply_image_results_with_failures(self, dynamic_data):
        dynamic = DynamicPost.from_raw(dynamic_data)

        results = [
            ImageDownloadResult(url="dyn1", local_path="dynamic/dyn_001_00.jpg", status="ok"),
            ImageDownloadResult(url="dyn2", local_path="dynamic/dyn_001_01.jpg", status="failed"),
        ]
        dynamic.apply_image_results(results)

        assert len(dynamic.image_locals) == 1
        assert "dynamic/dyn_001_00.jpg" in dynamic.image_locals

    def test_from_raw_empty_payload(self):
        dynamic = DynamicPost.from_raw({})

        assert dynamic.id_str == ""
        assert dynamic.type == ""
        assert dynamic.text == ""
        assert dynamic.timestamp is None
        assert dynamic.forwarded is None
        assert dynamic.image_urls == []


# ---------------------------------------------------------------------------
# Edge cases and integration tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_up_profile_with_empty_user_info(self):
        profile = UpProfile.from_raw({}, {}, {})

        assert profile.mid is None
        assert profile.name == ""
        assert profile.face_url == ""
        assert profile.social == {"following": 0, "follower": 0, "whisper": 0, "black": 0}
        assert profile.stats == {"archive_view": 0, "article_view": 0, "likes": 0}

    def test_video_detail_with_malformed_pages(self):
        raw = {
            "info": {
                "bvid": "BV1test",
                "pages": [{"cid": 1}, "not_a_dict", {"cid": 2, "part": "P2"}],
            },
        }
        video = VideoDetail.from_raw(raw)

        assert len(video.pages) == 2
        assert video.pages[0].cid == 1
        assert video.pages[1].cid == 2

    def test_article_with_duplicate_image_urls(self):
        list_item = {
            "id": 999,
            "title": "Dup Images",
            "image_urls": ["https://example.com/img1.jpg"],
            "origin_image_urls": ["https://example.com/img1.jpg", "https://example.com/img2.jpg"],
            "banner_url": "https://example.com/img1.jpg",
            "stats": {},
        }
        article = Article.from_raw(list_item, None, [])

        assert len(article.image_urls) == 2
        assert article.image_urls[0] == "https://example.com/img1.jpg"
        assert article.image_urls[1] == "https://example.com/img2.jpg"

    def test_opus_with_list_images_fallback(self):
        list_item = {
            "opus_id": "opus_list",
            "title": "List Images",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "pics": [{"url": "https://example.com/list1.jpg"}],
                        },
                    },
                },
            },
        }
        opus = OpusPost.from_raw(list_item, None)

        assert len(opus.images) == 1
        assert opus.images[0]["url"] == "https://example.com/list1.jpg"

        jobs = opus.collect_image_jobs(uid=12345)
        assert len(jobs) == 1
        assert jobs[0] == ("https://example.com/list1.jpg", "opus/opus_list_00.jpg")

    def test_dynamic_with_unknown_major_type(self):
        raw = {
            "id_str": "dyn_unknown",
            "type": "DYNAMIC_TYPE_UNKNOWN",
            "modules": {
                "module_author": {"pub_ts": 1700000000},
                "module_dynamic": {
                    "desc": {"text": "Unknown type"},
                    "major": {"type": "MAJOR_TYPE_UNKNOWN", "custom_field": "value"},
                },
            },
        }
        dynamic = DynamicPost.from_raw(raw)

        assert dynamic.id_str == "dyn_unknown"
        assert dynamic.major["type"] == "MAJOR_TYPE_UNKNOWN"
        assert dynamic.image_urls == []

    def test_all_models_item_id_property(self):
        profile = UpProfile(mid=123)
        video = VideoDetail(bvid="BV123")
        article = Article(id="456")
        opus = OpusPost(id="opus_789")
        dynamic = DynamicPost(id_str="dyn_999")

        assert profile.item_id == "123"
        assert video.item_id == "BV123"
        assert article.item_id == "456"
        assert opus.item_id == "opus_789"
        assert dynamic.item_id == "dyn_999"

    def test_image_download_result_dataclass(self):
        result = ImageDownloadResult(
            url="https://example.com/img.jpg",
            local_path="images/test.jpg",
            status="ok",
            error="",
        )

        assert result.url == "https://example.com/img.jpg"
        assert result.local_path == "images/test.jpg"
        assert result.status == "ok"
        assert result.error == ""

    def test_image_download_result_with_error(self):
        result = ImageDownloadResult(
            url="https://example.com/img.jpg",
            local_path="images/test.jpg",
            status="failed",
            error="HTTP 404",
        )

        assert result.status == "failed"
        assert result.error == "HTTP 404"
