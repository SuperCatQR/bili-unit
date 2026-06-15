# Tests for the three real-data quality fixes on OpusPost:
#   1. YAML frontmatter stripping on detail.markdown
#   2. Cover dict-shape tolerance ({url, width, height} or plain str)
#   3. Merged ``images`` field (was list_images + detail_images)
#
# See ``bili_unit/parsing/models/opus.py`` for the helpers under test.

from __future__ import annotations

from bili_unit.parsing._images import ImageDownloadResult
from bili_unit.parsing.models.opus import (
    OpusPost,
    _strip_yaml_frontmatter,
    _url_from_value,
)

# ---------------------------------------------------------------------------
# A. YAML frontmatter stripping
# ---------------------------------------------------------------------------

class TestOpusFrontmatterStripping:
    def test_no_frontmatter_unchanged(self):
        md = "# Title\n\nBody text."
        assert _strip_yaml_frontmatter(md) == md

    def test_simple_frontmatter_dropped(self):
        md = "---\nfoo: bar\nbaz: 1\n---\n\n# Title\n\nbody"
        assert _strip_yaml_frontmatter(md) == "# Title\n\nbody"

    def test_modules_frontmatter_dropped(self):
        md = (
            "---\n"
            "fallback: null\n"
            "item:\n"
            "  basic:\n"
            "    aigc: false\n"
            "  modules:\n"
            "  - module_type: MODULE_TYPE_AUTHOR\n"
            "    layer_config:\n"
            "      width: 12\n"
            "---\n"
            "\n"
            "# Real Title\n\nReal body content."
        )
        out = _strip_yaml_frontmatter(md)
        assert out == "# Real Title\n\nReal body content."

    def test_open_frontmatter_unchanged(self):
        # Defensive: leading ``---`` but no closing fence — leave alone.
        md = "---\nthis is not actually frontmatter\nno close"
        assert _strip_yaml_frontmatter(md) == md

    def test_empty_string(self):
        assert _strip_yaml_frontmatter("") == ""

    def test_only_three_dashes_unchanged(self):
        # Just ``---`` with no newline after — not a frontmatter opener.
        assert _strip_yaml_frontmatter("---") == "---"

    def test_frontmatter_no_blank_after_close(self):
        md = "---\nk: v\n---\n# Title"
        assert _strip_yaml_frontmatter(md) == "# Title"

    def test_frontmatter_terminating_at_eof(self):
        # Closing fence with no trailing newline.
        md = "---\nk: v\n---"
        assert _strip_yaml_frontmatter(md) == ""

    def test_real_world_modules_dump(self):
        # Mirrors the bilibili-api Opus.markdown() shape: huge frontmatter
        # dumping the modules dict, then the actual body. Asserting that
        # after stripping the body starts with the expected title.
        md = (
            "---\n"
            "fallback: null\n"
            "item:\n"
            "  basic:\n"
            "    aigc: false\n"
            "    article_type: 4\n"
            "    collection_id: '1049219'\n"
            "    comment_id_str: '47993683'\n"
            "  id_str: '1191456505301303346'\n"
            "  modules:\n"
            "  - module_author: null\n"
            "    module_blocked: null\n"
            "    module_type: 0\n"
            "---\n"
            "\n"
            "# 在野志征稿\n"
            "\n"
            "在宏大叙事与温情文学之外，"
            "中国广袤的基层土地上。"
        )
        out = _strip_yaml_frontmatter(md)
        assert out.startswith("# 在野志征稿")
        assert "fallback" not in out
        assert "module_type" not in out


# ---------------------------------------------------------------------------
# B. Cover dict-shape tolerance
# ---------------------------------------------------------------------------

class TestOpusCoverShape:
    def test_url_from_value_string(self):
        assert _url_from_value("http://x/y.jpg") == "http://x/y.jpg"

    def test_url_from_value_dict_with_url(self):
        v = {"url": "http://x/y.jpg", "width": 680, "height": 383}
        assert _url_from_value(v) == "http://x/y.jpg"

    def test_url_from_value_dict_without_url(self):
        assert _url_from_value({"width": 1, "height": 1}) == ""

    def test_url_from_value_dict_empty_url(self):
        assert _url_from_value({"url": ""}) == ""

    def test_url_from_value_none(self):
        assert _url_from_value(None) == ""

    def test_url_from_value_int(self):
        assert _url_from_value(42) == ""

    def test_url_from_value_list(self):
        assert _url_from_value(["http://x/y.jpg"]) == ""

    def test_from_raw_with_dict_cover(self):
        list_item = {
            "opus_id": "opus_d1",
            "title": "Dict Cover",
            "cover": {
                "url": "http://i0.hdslb.com/bfs/new_dyn/abc.png",
                "width": 680,
                "height": 383,
            },
            "modules": {},
        }
        opus = OpusPost.from_raw(list_item, None)

        assert opus.cover == "http://i0.hdslb.com/bfs/new_dyn/abc.png"
        jobs = opus.collect_image_jobs(uid=1)
        assert any(
            url == "http://i0.hdslb.com/bfs/new_dyn/abc.png" for url, _ in jobs
        )

    def test_from_raw_with_string_cover(self):
        list_item = {
            "opus_id": "opus_s1",
            "title": "String Cover",
            "cover": "http://x/y.jpg",
            "modules": {},
        }
        opus = OpusPost.from_raw(list_item, None)
        assert opus.cover == "http://x/y.jpg"

    def test_from_raw_with_dict_cover_missing_url(self):
        list_item = {
            "opus_id": "opus_no",
            "title": "No URL",
            "cover": {"width": 1, "height": 1},
            "modules": {},
        }
        opus = OpusPost.from_raw(list_item, None)
        assert opus.cover == ""

    def test_from_dict_with_dict_cover_migration(self):
        # Defensive against persisted dict-shaped cover (bug write-back path).
        d = {
            "id": "opus_pd",
            "opus_id": "opus_pd",
            "title": "Persisted dict",
            "cover": {"url": "http://x/y.jpg"},
            "stats": {},
        }
        opus = OpusPost.from_dict(d)
        assert opus.cover == "http://x/y.jpg"


# ---------------------------------------------------------------------------
# C. Merged ``images`` field
# ---------------------------------------------------------------------------

class TestOpusImagesMerge:
    def test_detail_images_win(self):
        list_item = {
            "opus_id": "opus_dwin",
            "title": "Detail Wins",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "pics": [{"url": "http://x/listing.jpg"}],
                        },
                    },
                },
            },
        }
        detail = {
            "markdown": "# Body",
            "images": [
                {"url": "http://x/d1.jpg", "width": 100, "height": 200},
                {"url": "http://x/d2.jpg", "width": 50, "height": 80},
            ],
        }
        opus = OpusPost.from_raw(list_item, detail)

        urls = [img["url"] for img in opus.images]
        # Detail entries first, listing-only URL appended after dedup.
        assert urls[0] == "http://x/d1.jpg"
        assert urls[1] == "http://x/d2.jpg"
        # Listing URL is distinct so it joins as a third entry.
        assert urls[2] == "http://x/listing.jpg"
        # Whitelist: width/height carry through; nothing else.
        assert opus.images[0] == {
            "url": "http://x/d1.jpg",
            "width": 100,
            "height": 200,
        }

    def test_listing_fallback_when_no_detail(self):
        list_item = {
            "opus_id": "opus_lf",
            "title": "List Fallback",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "pics": [
                                {"url": "http://x/a.jpg"},
                                {"url": "http://x/b.jpg"},
                            ],
                        },
                    },
                },
            },
        }
        opus = OpusPost.from_raw(list_item, None)

        assert opus.images == [
            {"url": "http://x/a.jpg"},
            {"url": "http://x/b.jpg"},
        ]

    def test_overlap_dedup(self):
        list_item = {
            "opus_id": "opus_dup",
            "title": "Dup",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {"pics": [{"url": "http://x/shared.jpg"}]},
                    },
                },
            },
        }
        detail = {
            "markdown": "",
            "images": [
                {"url": "http://x/shared.jpg", "width": 1, "height": 1},
            ],
        }
        opus = OpusPost.from_raw(list_item, detail)

        # No duplicate; detail's metadata is preserved.
        assert len(opus.images) == 1
        assert opus.images[0] == {
            "url": "http://x/shared.jpg",
            "width": 1,
            "height": 1,
        }

    def test_unknown_keys_dropped_from_detail(self):
        list_item = {"opus_id": "opus_uk", "title": "UK", "modules": {}}
        detail = {
            "markdown": "",
            "images": [
                {
                    "url": "http://x/y.jpg",
                    "width": 10,
                    "height": 20,
                    "size": 99999,
                    "tags": ["a"],
                },
            ],
        }
        opus = OpusPost.from_raw(list_item, detail)
        assert opus.images[0] == {
            "url": "http://x/y.jpg",
            "width": 10,
            "height": 20,
        }

    def test_apply_image_results_attaches_per_url(self):
        opus = OpusPost(
            id="opus_app",
            cover="http://x/cover.jpg",
            images=[
                {"url": "http://x/a.jpg"},
                {"url": "http://x/b.jpg"},
            ],
        )
        results = [
            ImageDownloadResult(
                url="http://x/cover.jpg",
                local_path="opus/opus_app_cover.jpg",
                status="ok",
            ),
            ImageDownloadResult(
                url="http://x/a.jpg",
                local_path="opus/opus_app_00.jpg",
                status="ok",
            ),
            ImageDownloadResult(
                url="http://x/b.jpg",
                local_path="opus/opus_app_01.jpg",
                status="skipped",
            ),
        ]
        opus.apply_image_results(results)

        assert opus.cover_local == "opus/opus_app_cover.jpg"
        assert opus.images[0]["local_path"] == "opus/opus_app_00.jpg"
        assert opus.images[1]["local_path"] == "opus/opus_app_01.jpg"

    def test_cover_fail_does_not_corrupt_cover_local(self):
        # Regression: previously the first-result-is-cover positional logic
        # would misassign content paths to cover_local when the cover failed.
        opus = OpusPost(
            id="opus_cf",
            cover="http://x/cover.jpg",
            images=[
                {"url": "http://x/a.jpg"},
                {"url": "http://x/b.jpg"},
            ],
        )
        results = [
            ImageDownloadResult(
                url="http://x/cover.jpg",
                local_path="opus/opus_cf_cover.jpg",
                status="failed",
                error="HTTP 404",
            ),
            ImageDownloadResult(
                url="http://x/a.jpg",
                local_path="opus/opus_cf_00.jpg",
                status="ok",
            ),
            ImageDownloadResult(
                url="http://x/b.jpg",
                local_path="opus/opus_cf_01.jpg",
                status="ok",
            ),
        ]
        opus.apply_image_results(results)

        # Cover stays empty — must NOT inherit a content path.
        assert opus.cover_local == ""
        assert opus.images[0]["local_path"] == "opus/opus_cf_00.jpg"
        assert opus.images[1]["local_path"] == "opus/opus_cf_01.jpg"

    def test_round_trip_images(self):
        opus = OpusPost(
            id="opus_rt",
            cover="http://x/c.jpg",
            cover_local="opus/opus_rt_cover.jpg",
            images=[
                {
                    "url": "http://x/a.jpg",
                    "width": 100,
                    "height": 200,
                    "local_path": "opus/opus_rt_00.jpg",
                },
                {"url": "http://x/b.jpg"},
            ],
        )
        d = opus.to_dict()
        assert "list_images" not in d
        assert "detail_images" not in d
        assert "image_locals" not in d

        restored = OpusPost.from_dict(d)
        assert restored.images == opus.images
        assert restored.cover_local == "opus/opus_rt_cover.jpg"

    def test_v1_migration_full(self):
        # Persisted v1 JSON: list_images + detail_images + image_locals.
        d = {
            "_schema_version": 1,
            "id": "opus_v1",
            "opus_id": "opus_v1",
            "title": "V1 Migrate",
            "cover": "http://x/c.jpg",
            "stats": {},
            "list_images": ["http://x/legacy_a.jpg", "http://x/legacy_b.jpg"],
            "detail_images": [
                {"url": "http://x/d_a.jpg", "width": 1, "height": 2},
                {"url": "http://x/d_b.jpg"},
            ],
            "image_locals": [
                "opus/opus_v1_00.jpg",
                "opus/opus_v1_01.jpg",
            ],
            "cover_local": "opus/opus_v1_cover.jpg",
        }
        opus = OpusPost.from_dict(d)

        # detail_images wins because it's a non-empty list of dicts.
        assert [img["url"] for img in opus.images] == [
            "http://x/d_a.jpg",
            "http://x/d_b.jpg",
        ]
        # Positional image_locals are pinned onto the new entries.
        assert opus.images[0]["local_path"] == "opus/opus_v1_00.jpg"
        assert opus.images[1]["local_path"] == "opus/opus_v1_01.jpg"
        # Width/height preserved on the entries that had them.
        assert opus.images[0]["width"] == 1
        assert opus.images[0]["height"] == 2
        assert opus.cover_local == "opus/opus_v1_cover.jpg"

    def test_v1_migration_list_only(self):
        # Old data with only list_images (no detail enrichment).
        d = {
            "id": "opus_lo",
            "opus_id": "opus_lo",
            "title": "List Only",
            "stats": {},
            "list_images": ["http://x/a.jpg", "http://x/b.jpg"],
            "image_locals": ["opus/opus_lo_00.jpg"],
        }
        opus = OpusPost.from_dict(d)

        assert [img["url"] for img in opus.images] == [
            "http://x/a.jpg",
            "http://x/b.jpg",
        ]
        assert opus.images[0]["local_path"] == "opus/opus_lo_00.jpg"
        # Second entry has no paired local — no key set.
        assert "local_path" not in opus.images[1]
