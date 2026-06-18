# Tests for bili_unit.parsing.command.ParsingCommand on the SQLite stack.
#
# ParsingCommand now takes only ``BiliSettings`` and constructs UidContext /
# ParsingStore / FetchingStore on each parse_uid call. The old ParsingQuery
# layer was deleted in Phase 3.2 and replaced with direct store reads, so
# the legacy "ParsingQuery DTO" tests (~13 of them) were dropped — they
# tested an interface that no longer exists. The ``failed_item_ids`` task
# field is also gone (see ``parsing/__init__.py``); related assertions
# were dropped for the same reason.
#
# The status-rollup tests below patch ``ParsingMaterializer.parse_model``
# at class level so the orchestration logic can be exercised without
# having to seed every model's raw payloads. The end-to-end integration
# test at the bottom drives the real materializer over seeded
# FetchingStore rows to confirm the wiring.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching._store import FetchingStore
from bili_unit.parsing import (
    ParsingCommandResult,
    ParsingModelStatus,
    ParsingTaskStatus,
)
from bili_unit.parsing._store import ParsingStore
from bili_unit.parsing.command import ParsingCommand

EXPECTED_MODEL_ORDER = (
    "user_profile",
    "video_work",
    "video_subtitle",
    "article_post",
    "opus_post",
    "dynamic_event",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(bili_db_dir=str(tmp_path))


@pytest.fixture
def parsing_command(settings: BiliSettings) -> ParsingCommand:
    return ParsingCommand(settings)


async def _read_task(uid: int, settings: BiliSettings) -> dict | None:
    """Open a fresh UidContext and return the parsing stage_task payload
    along with its status column."""
    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open()
    try:
        row = await ctx.main.fetch_one(
            "SELECT status, payload FROM stage_task WHERE stage = 'parsing'",
        )
        if row is None:
            return None
        return {"status": row["status"], **json.loads(row["payload"])}
    finally:
        await ctx.close()


async def _list_stage_events(uid: int, settings: BiliSettings) -> list[dict]:
    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        rows = await ctx.main.fetch_all(
            "SELECT e.event, e.level, e.stage, e.item_type, e.item_id, e.data_json "
            "FROM stage_event e "
            "JOIN stage_run r ON r.run_id = e.run_id "
            "WHERE r.uid = ? ORDER BY e.id",
            (uid,),
        )
        return [
            {
                "event": row["event"],
                "level": row["level"],
                "stage": row["stage"],
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "data": json.loads(row["data_json"] or "{}"),
            }
            for row in rows
        ]
    finally:
        await ctx.close()


async def _list_stage_runs(uid: int, settings: BiliSettings) -> list[dict]:
    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open(raw=False)
    try:
        rows = await ctx.main.fetch_all(
            "SELECT run_id, command, status, args_json, summary_json "
            "FROM stage_run WHERE uid = ? ORDER BY started_at_ms",
            (uid,),
        )
        return [
            {
                "run_id": row["run_id"],
                "command": row["command"],
                "status": row["status"],
                "args": json.loads(row["args_json"] or "{}"),
                "summary": json.loads(row["summary_json"] or "{}"),
            }
            for row in rows
        ]
    finally:
        await ctx.close()


# ---------------------------------------------------------------------------
# parse_uid — status rollup over per-model counts
# ---------------------------------------------------------------------------

async def test_parse_uid_all_models_succeed_status_success(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """All models return positive counts → overall SUCCESS, every model
    SUCCESS in the task payload, counts persisted."""
    counts = {
        "user_profile": 1,
        "video_work": 5,
        "video_subtitle": 5,
        "article_post": 3,
        "opus_post": 2,
        "dynamic_event": 4,
    }

    async def fake_parse_model(self, uid, model_name, mode):
        return counts[model_name]

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(uid=1001, mode="full")

    assert isinstance(result, ParsingCommandResult)
    assert result.uid == 1001
    assert result.status == ParsingTaskStatus.SUCCESS
    assert result.run_id

    task = await _read_task(1001, settings)
    assert task is not None
    assert task["status"] == ParsingTaskStatus.SUCCESS.value

    models = task["models"]
    assert tuple(models.keys()) == EXPECTED_MODEL_ORDER
    for name in EXPECTED_MODEL_ORDER:
        assert models[name]["status"] == ParsingModelStatus.SUCCESS.value
        assert models[name]["count"] == counts[name]

    runs = await _list_stage_runs(1001, settings)
    assert len(runs) == 1
    assert runs[0]["run_id"] == result.run_id
    assert runs[0]["command"] == "parse"
    assert runs[0]["status"] == "SUCCESS"
    assert runs[0]["args"] == {
        "mode": "full",
        "models": None,
        "download_images": False,
    }
    assert runs[0]["summary"]["status"] == "SUCCESS"

    events = await _list_stage_events(1001, settings)
    event_names = [event["event"] for event in events]
    assert event_names[0] == "parse.run.started"
    assert event_names[-1] == "parse.run.completed"
    assert event_names.count("parse.model.started") == len(EXPECTED_MODEL_ORDER)
    assert event_names.count("parse.model.completed") == len(EXPECTED_MODEL_ORDER)


async def test_parse_uid_zero_count_in_full_mode_stays_success(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """A model returning zero can mean a valid empty result, not a failure."""
    counts = {
        "user_profile": 1,
        "video_work": 0,
        "video_subtitle": 0,
        "article_post": 2,
        "opus_post": 0,
        "dynamic_event": 3,
    }

    async def fake_parse_model(self, uid, model_name, mode):
        return counts[model_name]

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(uid=2002)

    assert result.status == ParsingTaskStatus.SUCCESS

    task = await _read_task(2002, settings)
    assert task is not None
    assert task["status"] == ParsingTaskStatus.SUCCESS.value

    # Zero-count models are still SUCCESS at the model level — they ran fine,
    # just found nothing.
    assert task["models"]["video_work"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task["models"]["video_work"]["count"] == 0
    assert task["models"]["opus_post"]["count"] == 0
    # And positive-count models report what the materializer returned.
    assert task["models"]["user_profile"]["count"] == 1
    assert task["models"]["article_post"]["count"] == 2
    assert task["models"]["dynamic_event"]["count"] == 3


async def test_parse_uid_model_failure_marks_failed_and_partial(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """One model raising → that model FAILED, others SUCCESS, overall
    PARTIAL. The error must not propagate out of parse_uid."""

    async def fake_parse_model(self, uid, model_name, mode):
        if model_name == "article_post":
            raise RuntimeError("simulated parse failure")
        return 1

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(uid=3003)

    assert result.status == ParsingTaskStatus.PARTIAL

    task = await _read_task(3003, settings)
    assert task is not None
    assert task["models"]["article_post"]["status"] == ParsingModelStatus.FAILED.value
    for name in EXPECTED_MODEL_ORDER:
        if name == "article_post":
            continue
        assert task["models"][name]["status"] == ParsingModelStatus.SUCCESS.value

    runs = await _list_stage_runs(3003, settings)
    assert runs[0]["status"] == "PARTIAL"

    events = await _list_stage_events(3003, settings)
    failed = [event for event in events if event["event"] == "parse.model.failed"]
    assert len(failed) == 1
    assert failed[0]["item_id"] == "article_post"
    assert failed[0]["data"]["error_type"] == "RuntimeError"


async def test_parse_uid_missing_required_raw_marks_model_skipped(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    result = await parsing_command.parse_uid(uid=3103, models=["video_work"])

    assert result.status == ParsingTaskStatus.PARTIAL

    task = await _read_task(3103, settings)
    assert task is not None
    assert task["models"]["video_work"] == {
        "status": ParsingModelStatus.SKIPPED.value,
        "count": 0,
    }

    events = await _list_stage_events(3103, settings)
    skipped = [event for event in events if event["event"] == "parse.model.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["item_id"] == "video_work"
    assert skipped[0]["data"]["missing_endpoints"] == ["video_detail"]


async def test_parse_uid_incremental_mode_with_existing_rows_stays_success(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """Incremental mode: when a model returns count=0 but already has rows
    in the DB (i.e. nothing new to parse, but historical data exists), the
    overall status stays SUCCESS — the "no fetch payload" branch is
    suppressed by the existing-items check."""
    uid = 4242

    # Pre-seed user_profile so get_existing_item_ids("user_profile") is
    # non-empty during the post-zero-check branch.
    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open()
    try:
        from bili_unit.parsing.models.up_profile import UpProfile
        from bili_unit.parsing.models.video_detail import VideoDetail

        ps = ParsingStore(ctx)
        await ps.save_user_profile(UpProfile(mid=uid, name="seed"))
        await ps.save_video(VideoDetail(bvid="BVseed", title="seed"))
    finally:
        await ctx.close()

    async def fake_parse_model(self, uid_arg, model_name, mode):
        # Every model claims it had nothing new — this mimics a real
        # incremental run where all items are already parsed.
        return 0

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(uid=uid, mode="incremental")

    assert result.status == ParsingTaskStatus.SUCCESS


async def test_parse_uid_with_download_images_calls_downloader(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """download_images=True triggers ParsingMaterializer.download_images
    after model parsing and persists the summary into the task payload."""

    async def fake_parse_model(self, uid, model_name, mode):
        return 1

    fake_summary = {
        "total": 3, "ok": 2, "skipped": 1, "failed": 0, "failed_urls": [],
    }
    download_mock = AsyncMock(return_value=fake_summary)

    with (
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.parse_model",
            new=fake_parse_model,
        ),
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.download_images",
            new=download_mock,
        ),
    ):
        result = await parsing_command.parse_uid(uid=4004, download_images=True)

    assert result.status == ParsingTaskStatus.SUCCESS
    download_mock.assert_awaited_once_with(4004)

    task = await _read_task(4004, settings)
    assert task is not None
    assert task["images"] == fake_summary


async def test_parse_uid_without_download_images_does_not_call_downloader(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """download_images=False (default) skips the downloader entirely; the
    images block in the task payload stays None."""

    async def fake_parse_model(self, uid, model_name, mode):
        return 1

    download_mock = AsyncMock()

    with (
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.parse_model",
            new=fake_parse_model,
        ),
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.download_images",
            new=download_mock,
        ),
    ):
        await parsing_command.parse_uid(uid=5005, download_images=False)

    download_mock.assert_not_awaited()

    task = await _read_task(5005, settings)
    assert task is not None
    assert task["images"] is None


async def test_parse_uid_download_images_failure_does_not_change_status(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """An exception out of download_images is logged, swallowed, and does
    not contaminate the model-level status rollup."""

    async def fake_parse_model(self, uid, model_name, mode):
        return 1

    with (
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.parse_model",
            new=fake_parse_model,
        ),
        patch(
            "bili_unit.parsing.command.ParsingMaterializer.download_images",
            new=AsyncMock(side_effect=RuntimeError("dl fail")),
        ),
    ):
        result = await parsing_command.parse_uid(uid=6006, download_images=True)

    # All models succeeded with positive counts → SUCCESS regardless of the
    # image download outcome.
    assert result.status == ParsingTaskStatus.SUCCESS

    task = await _read_task(6006, settings)
    assert task is not None
    # images block remains None because the exception fired before the
    # update_task_images call.
    assert task["images"] is None


# ---------------------------------------------------------------------------
# parse_uid — passes mode through to the materializer.
# ---------------------------------------------------------------------------

async def test_parse_uid_passes_mode_through_to_materializer(
    parsing_command: ParsingCommand,
):
    """The ``mode`` argument should reach every per-model materializer call
    unchanged — full vs incremental gates the materializer's skip logic."""
    seen: list[tuple[int, str, str]] = []

    async def fake_parse_model(self, uid, model_name, mode):
        seen.append((uid, model_name, mode))
        return 1

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        await parsing_command.parse_uid(uid=7007, mode="incremental")

    assert {entry[2] for entry in seen} == {"incremental"}
    assert [entry[1] for entry in seen] == list(EXPECTED_MODEL_ORDER)


async def test_parse_uid_can_run_explicit_model_subset(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    seen: list[str] = []

    async def fake_parse_model(self, uid, model_name, mode):
        seen.append(model_name)
        return 1

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(
            uid=7107,
            models=["video_work", "opus_post"],
        )

    assert result.status == ParsingTaskStatus.SUCCESS
    assert seen == ["video_work", "opus_post"]

    task = await _read_task(7107, settings)
    assert task is not None
    assert tuple(task["models"].keys()) == ("video_work", "opus_post")


async def test_parse_uid_sorts_explicit_model_subset_by_dependency_order(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    seen: list[str] = []

    async def fake_parse_model(self, uid, model_name, mode):
        seen.append(model_name)
        return 1

    with patch(
        "bili_unit.parsing.command.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await parsing_command.parse_uid(
            uid=7109,
            models=["video_subtitle", "video_work"],
        )

    assert result.status == ParsingTaskStatus.SUCCESS
    assert seen == ["video_work", "video_subtitle"]

    task = await _read_task(7109, settings)
    assert task is not None
    assert tuple(task["models"].keys()) == ("video_work", "video_subtitle")


async def test_parse_uid_rejects_unknown_model(
    parsing_command: ParsingCommand,
):
    with pytest.raises(ValueError):
        await parsing_command.parse_uid(uid=7108, models=["video_work", "typo"])


# ---------------------------------------------------------------------------
# End-to-end — real materializer over seeded raw payloads
# ---------------------------------------------------------------------------

async def test_parse_uid_end_to_end_writes_typed_rows(
    parsing_command: ParsingCommand, settings: BiliSettings,
):
    """Drive the real ParsingMaterializer through ParsingCommand by seeding
    the FetchingStore raw DB with one minimal payload per model that has a
    listing endpoint. The user_profile and video_work models are sufficient
    to confirm the wiring; the remaining four return 0 (no listing seeded)
    and trigger PARTIAL — that's exactly the expected behaviour for a
    fresh uid with only those two endpoints fetched."""
    uid = 9001

    # Seed the raw DB before the command runs.
    seed_ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await seed_ctx.open()
    try:
        fetch = FetchingStore(seed_ctx)

        # user_profile required raws.
        await fetch.save_raw_payload(
            "user_info", "",
            {"mid": uid, "name": "real", "face": "https://example.com/f.jpg"},
        )
        await fetch.save_raw_payload(
            "relation_info", "", {"following": 1, "follower": 2},
        )
        await fetch.save_raw_payload(
            "up_stat", "", {"archive": {"view": 10}, "article": {"view": 0}, "likes": 0},
        )

        # video_work fanout — one bvid.
        await fetch.save_raw_payload(
            "video_detail", "BV1real",
            {
                "info": {
                    "bvid": "BV1real",
                    "title": "Real Video",
                    "pages": [],
                    "stat": {},
                    "owner": {},
                },
                "tags": [],
            },
        )
    finally:
        await seed_ctx.close()

    result = await parsing_command.parse_uid(uid=uid, mode="full")

    # 4 of 6 models produced 0 rows in full mode → PARTIAL.
    assert result.status == ParsingTaskStatus.PARTIAL

    # Verify rows actually landed in the parsing main DB.
    read_ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await read_ctx.open()
    try:
        ps = ParsingStore(read_ctx)
        assert await ps.get_existing_item_ids("user_profile") == {str(uid)}
        assert await ps.get_existing_item_ids("video_work") == {"BV1real"}

        profile = await ps.get_user_profile_payload(uid)
        assert profile is not None
        assert profile["name"] == "real"

        video = await ps.get_video_payload("BV1real")
        assert video is not None
        assert video["title"] == "Real Video"
    finally:
        await read_ctx.close()

    task = await _read_task(uid, settings)
    assert task is not None
    assert task["models"]["user_profile"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task["models"]["user_profile"]["count"] == 1
    assert task["models"]["video_work"]["status"] == ParsingModelStatus.SUCCESS.value
    assert task["models"]["video_work"]["count"] == 1
