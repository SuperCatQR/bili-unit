# Tests for parsing-layer infrastructure: spec registry and the
# ParsingMaterializer's incremental skip path against the new
# (ctx, parse_store, fetch_store) signature introduced in Phase 3.2.

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit.fetching._store import FetchingStore
from bili_unit.parsing._images import ImageDownloadResult
from bili_unit.parsing._store import ParsingStore
from bili_unit.parsing.materializer import ParsingMaterializer
from bili_unit.parsing.models.up_profile import UpProfile
from bili_unit.parsing.models.video_detail import VideoDetail
from bili_unit.parsing.specs import MODEL_ORDER, get_spec, iter_specs

UID = 4242


# ---------------------------------------------------------------------------
# Spec registry — pure unit-level assertions, no infrastructure required.
# ---------------------------------------------------------------------------

def test_parsing_specs_register_existing_models():
    assert MODEL_ORDER == (
        "user_profile",
        "video_work",
        "video_subtitle",
        "article_post",
        "opus_post",
        "dynamic_event",
    )

    handlers = {spec.name: spec.materializer_handler for spec in iter_specs()}
    assert set(handlers) == set(MODEL_ORDER)
    assert handlers["user_profile"] == "_parse_user_profile"
    assert handlers["dynamic_event"] == "_parse_dynamic_events"
    assert handlers["video_subtitle"] == "_parse_video_subtitle"
    assert all(handler.startswith("_parse_") for handler in handlers.values())
    assert get_spec("user_profile").singleton is True
    assert get_spec("video_work").parser_cls().__name__ == "VideoDetail"
    assert get_spec("dynamic_event").parser_cls().__name__ == "DynamicPost"


# ---------------------------------------------------------------------------
# Materializer fixtures — open a real UidContext on tmp_path and share it
# between the parsing/fetching stores so the materializer reads from the
# same SQLite databases the tests seed.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def stores(tmp_path: Path):
    ctx = UidContext(uid=UID, root=tmp_path)
    await ctx.open()
    try:
        yield ctx, ParsingStore(ctx), FetchingStore(ctx)
    finally:
        await ctx.close()


# ---------------------------------------------------------------------------
# Materializer — incremental mode skip rules. Full-parse paths are covered
# by the per-model materializer tests in test_parsing_video_subtitle.py and
# the end-to-end orchestration tests in test_parsing_command.py.
# ---------------------------------------------------------------------------

async def test_incremental_user_profile_skips_when_parsed_row_is_fresh(stores):
    """A parsed row newer than its raw inputs is skipped in incremental mode."""
    ctx, parse_store, fetch_store = stores

    existing = UpProfile(mid=UID, name="already parsed")
    await parse_store.save_user_profile(existing)

    # Seed every required raw payload — if the materializer wrongly reads
    # them in incremental mode, the assertion below would catch it because
    # ``UpProfile.from_raw`` would overwrite the saved name.
    await fetch_store.save_raw_payload(
        "user_info", "", {"mid": UID, "name": "fresh"}, fetched_at_ms=1,
    )
    await fetch_store.save_raw_payload(
        "relation_info", "", {"following": 0, "follower": 0}, fetched_at_ms=1,
    )
    await fetch_store.save_raw_payload("up_stat", "", {}, fetched_at_ms=1)

    materializer = ParsingMaterializer(
        ctx=ctx, parse_store=parse_store, fetch_store=fetch_store,
    )

    count = await materializer.parse_model(UID, "user_profile", "incremental")
    assert count == 0

    payload = await parse_store.get_user_profile_payload(UID)
    assert payload is not None
    assert payload["name"] == "already parsed"


async def test_incremental_video_work_skips_only_fresh_existing_items(stores):
    """Fresh existing rows are preserved; new or stale rows are parsed."""
    ctx, parse_store, fetch_store = stores

    old = VideoDetail(bvid="BVold", title="old title")
    await parse_store.save_video(old)

    fresh_raw = {
        "info": {
            "bvid": "BVnew",
            "title": "BVnew fresh",
            "pages": [],
            "stat": {},
            "owner": {},
        },
        "tags": [],
    }
    # BVold's raw is older than the parsed row, so it is skipped.
    await fetch_store.save_raw_payload(
        "video_detail", "BVold",
        {"info": {"bvid": "BVold", "title": "stale"}, "tags": []},
        fetched_at_ms=1,
    )
    await fetch_store.save_raw_payload("video_detail", "BVnew", fresh_raw)

    materializer = ParsingMaterializer(
        ctx=ctx, parse_store=parse_store, fetch_store=fetch_store,
    )

    count = await materializer.parse_model(UID, "video_work", "incremental")
    assert count == 1

    old_payload = await parse_store.get_video_payload("BVold")
    new_payload = await parse_store.get_video_payload("BVnew")
    assert old_payload is not None
    assert old_payload["title"] == "old title"
    assert new_payload is not None
    assert new_payload["title"] == "BVnew fresh"


async def test_incremental_video_work_reparses_when_raw_is_newer(stores):
    ctx, parse_store, fetch_store = stores

    old = VideoDetail(bvid="BVold", title="old title")
    await parse_store.save_video(old)
    await fetch_store.save_raw_payload(
        "video_detail",
        "BVold",
        {
            "info": {
                "bvid": "BVold",
                "title": "fresh title",
                "pages": [],
                "stat": {},
                "owner": {},
            },
            "tags": [],
        },
        fetched_at_ms=9_999_999_999_999,
    )

    materializer = ParsingMaterializer(
        ctx=ctx, parse_store=parse_store, fetch_store=fetch_store,
    )

    count = await materializer.parse_model(UID, "video_work", "incremental")

    assert count == 1
    payload = await parse_store.get_video_payload("BVold")
    assert payload is not None
    assert payload["title"] == "fresh title"


async def test_full_mode_user_profile_overwrites_existing(stores):
    """Sanity counterpart: the same pre-seeded row is re-parsed in ``full``
    mode and the new payload from raw wins. Confirms the skip is mode-gated."""
    ctx, parse_store, fetch_store = stores

    existing = UpProfile(mid=UID, name="stale")
    await parse_store.save_user_profile(existing)

    await fetch_store.save_raw_payload(
        "user_info",
        "",
        {"mid": UID, "name": "fresh", "face": ""},
    )
    await fetch_store.save_raw_payload(
        "relation_info", "", {"following": 1, "follower": 2},
    )
    await fetch_store.save_raw_payload("up_stat", "", {"archive": {"view": 0}})

    materializer = ParsingMaterializer(
        ctx=ctx, parse_store=parse_store, fetch_store=fetch_store,
    )

    count = await materializer.parse_model(UID, "user_profile", "full")
    assert count == 1

    payload = await parse_store.get_user_profile_payload(UID)
    assert payload is not None
    assert payload["name"] == "fresh"


async def test_image_download_skips_existing_db_blob(stores):
    ctx, parse_store, fetch_store = stores
    materializer = ParsingMaterializer(
        ctx=ctx, parse_store=parse_store, fetch_store=fetch_store,
    )
    cached_url = "https://example.com/cached.jpg"
    fresh_url = "https://example.com/fresh.jpg"
    await parse_store.save_image_asset(
        url=cached_url,
        source_kind="video.cover",
        source_id="BVcached",
        file_path="video/BVcached_cover.jpg",
        bytes=6,
        status="ok",
        data=b"cached",
    )

    class FakeDownloader:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, str]] = []

        async def download_many(
            self, jobs: list[tuple[str, str]],
        ) -> list[ImageDownloadResult]:
            self.jobs = jobs
            return [
                ImageDownloadResult(
                    url=url,
                    local_path=dest_rel,
                    status="ok",
                    data=b"fresh",
                )
                for url, dest_rel in jobs
            ]

    downloader = FakeDownloader()
    results = await materializer._download_missing_images(
        downloader,
        [
            (cached_url, "video/BVcached_cover.jpg"),
            (fresh_url, "video/BVfresh_cover.jpg"),
        ],
    )

    assert downloader.jobs == [(fresh_url, "video/BVfresh_cover.jpg")]
    assert [result.status for result in results] == ["skipped", "ok"]
    assert [result.data for result in results] == [b"cached", b"fresh"]
