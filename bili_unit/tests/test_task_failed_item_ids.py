"""Tests for the post-refactor "what failed?" derivation paths.

Phase 6 rewrite. Background:

The pre-refactor test file exercised a ``failed_item_ids`` field on each
stage's TaskDTO. After the SQLite migration, **that field is gone** — the
DTOs returned by the write-side commands carry only ``uid`` and ``status``.
Consumers that need to ask "what work failed?" now query the SQLite database
directly (against ``stage_error``), or use the dedicated store helper
(``FetchingStore.list_failed_items``).

Most of the original test cases have been dropped because they validated a
DTO surface that no longer exists. The tests below salvage the underlying
reasoning that still applies: an item that failed and was later retried to
success must NOT surface as "still failed".

Stages covered:
  * fetching — uses :meth:`FetchingStore.list_failed_items`, which both
    derives failed item_ids from ``stage_error`` and drops items with a
    later ``raw_payload`` row.
  * parsing — model-level granularity via the ``stage_task[stage='parsing']``
    payload (parsing does not use ``stage_error``).
  * processing — direct SQL against ``stage_error`` filtered by the
    ``audio_transcription`` table, demonstrating the consumer pattern.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit._env import BiliSettings
from bili_unit.fetching._store import FetchingStore
from bili_unit.parsing import ParsingModelStatus, ParsingTaskStatus
from bili_unit.parsing.command import ParsingCommand

# ---------------------------------------------------------------------------
# Shared UidContext fixture (request-scoped, on tmp_path).
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def ctx(tmp_path: Path):
    c = UidContext(uid=1001, root=tmp_path)
    await c.open()
    try:
        yield c
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Fetching: derive failed items from stage_error
# ---------------------------------------------------------------------------

async def test_fetching_failed_items_from_stage_error(ctx: UidContext) -> None:
    """A consumer querying ``stage_error`` for fetching sees the failed
    item_ids; the FetchingStore helper returns the same set."""
    store = FetchingStore(ctx)
    bvid = "BV1abc"

    await store.record_error(
        endpoint="video_detail",
        error_type="Http412Error",
        message="rate limited",
        retryable=False,
        detail={"item_id": bvid},
    )

    # Direct SQL — what an external consumer would do.
    rows = await ctx.main.fetch_all(
        "SELECT detail FROM stage_error "
        "WHERE stage = 'fetching' AND endpoint = ?",
        ("video_detail",),
    )
    assert len(rows) == 1

    # Helper — what the runner uses internally.
    failed = await store.list_failed_items("video_detail")
    assert failed == [bvid]


async def test_fetching_failed_items_drops_retry_to_success(
    ctx: UidContext,
) -> None:
    """An item that errored once but later got a SUCCESS ``raw_payload``
    row must NOT surface in the failed-items list. This used to be the
    ``failed_item_ids`` "drops retry-to-success" regression test; the
    behaviour now lives in :meth:`FetchingStore.list_failed_items`."""
    store = FetchingStore(ctx)
    bvid_ok = "BV1retry"
    bvid_bad = "BV1bad"

    await store.record_error(
        endpoint="video_detail",
        error_type="Http412Error",
        message="rate limited (since cleared)",
        retryable=True,
        detail={"item_id": bvid_ok},
    )
    # The retry succeeded — payload is now in raw_payload.
    await store.save_raw_payload(
        "video_detail", bvid_ok, {"info": {"bvid": bvid_ok}},
    )
    # And a different item is still failing.
    await store.record_error(
        endpoint="video_detail",
        error_type="Http412Error",
        message="still failing",
        retryable=False,
        detail={"item_id": bvid_bad},
    )

    failed = await store.list_failed_items("video_detail")
    assert failed == [bvid_bad]


# ---------------------------------------------------------------------------
# Parsing: derive failed models from stage_task[stage='parsing']
# ---------------------------------------------------------------------------

async def test_parsing_failed_models_in_stage_task_payload(
    tmp_path: Path,
) -> None:
    """When a model raises, the parsing stage records its name with status
    FAILED inside ``stage_task[stage='parsing'].payload.models``. A consumer
    querying that JSON sees exactly which models failed.

    This replaces the old ``DTO.failed_item_ids == ['article_post']`` test
    against ``stage_task`` JSON instead of a DTO field."""
    settings = BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "ptemp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr"),
        bili_processing_asr_backend="mock",
    )
    cmd = ParsingCommand(settings)
    uid = 2001

    # Patch parse_model so all models report 1 row except article_post which
    # raises; this is what produced the original "article_post failed" test.
    async def fake_parse_model(self, uid_, model_name, mode):  # noqa: ANN001
        if model_name == "article_post":
            raise RuntimeError("simulated parse failure")
        return 1

    with patch(
        "bili_unit.parsing.materializer.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        result = await cmd.parse_uid(uid=uid, mode="full")

    assert result.status == ParsingTaskStatus.PARTIAL

    # Inspect the persisted task payload directly via the main DB.
    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open()
    try:
        payload_json = await ctx.main.fetch_value(
            "SELECT payload FROM stage_task WHERE stage = 'parsing'",
        )
    finally:
        await ctx.close()

    import json
    payload = json.loads(payload_json)
    models = payload["models"]
    assert models["article_post"]["status"] == ParsingModelStatus.FAILED.value
    # Sanity: another model is reported SUCCESS so we know the failure is
    # localised, not a global "everything failed" effect.
    assert models["video_work"]["status"] == ParsingModelStatus.SUCCESS.value

    # Consumers that just want the failed model names use a simple list comp.
    failed_models = [
        name for name, entry in models.items()
        if entry.get("status") == ParsingModelStatus.FAILED.value
    ]
    assert failed_models == ["article_post"]


async def test_parsing_no_failed_models_when_all_pass(tmp_path: Path) -> None:
    """All models succeed → no FAILED entries in the task payload."""
    settings = BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "ptemp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr"),
        bili_processing_asr_backend="mock",
    )
    cmd = ParsingCommand(settings)
    uid = 2002

    async def fake_parse_model(self, uid_, model_name, mode):  # noqa: ANN001
        return 1

    with patch(
        "bili_unit.parsing.materializer.ParsingMaterializer.parse_model",
        new=fake_parse_model,
    ):
        await cmd.parse_uid(uid=uid, mode="full")

    ctx = UidContext(uid=uid, root=settings.bili_db_dir)
    await ctx.open()
    try:
        payload_json = await ctx.main.fetch_value(
            "SELECT payload FROM stage_task WHERE stage = 'parsing'",
        )
    finally:
        await ctx.close()

    import json
    models = json.loads(payload_json)["models"]
    failed = [
        name for name, entry in models.items()
        if entry.get("status") == ParsingModelStatus.FAILED.value
    ]
    assert failed == []


# ---------------------------------------------------------------------------
# Processing: derive failed bvids by joining stage_error with audio_transcription
# ---------------------------------------------------------------------------

async def test_processing_failed_items_from_stage_error_and_audio(
    ctx: UidContext,
) -> None:
    """A consumer asks "which bvids are still failing audio?" by joining
    ``stage_error`` (forensic log) with ``audio_transcription`` (current
    truth). Items that errored historically but later succeeded drop out.

    Replaces the old ``ProcessingRunner._collect_failed_item_ids`` test —
    same reasoning, expressed as the SQL the consumer would run."""
    bvid_bad = "BV1bad"
    bvid_ok = "BV1ok"

    # Seed a placeholder video row so audio_transcription's FK is satisfied.
    await ctx.main.execute(
        "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (bvid_bad, "bad", "{}", 1),
    )
    await ctx.main.execute(
        "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (bvid_ok, "ok", "{}", 2),
    )

    # Both bvids have a historical error record.
    await ctx.main.execute(
        "INSERT INTO stage_error("
        "    stage, pipeline, item_type, item_id, error_type, message, "
        "    retryable, occurred_at_ms"
        ") VALUES ('asr', 'audio', 'transcription', ?, ?, ?, ?, ?)",
        (bvid_bad, "RuntimeError", "permanent", 0, 100),
    )
    await ctx.main.execute(
        "INSERT INTO stage_error("
        "    stage, pipeline, item_type, item_id, error_type, message, "
        "    retryable, occurred_at_ms"
        ") VALUES ('asr', 'audio', 'transcription', ?, ?, ?, ?, ?)",
        (bvid_ok, "RuntimeError", "transient 401", 1, 200),
    )
    # Plus a uid-level error with no item_id — must NOT surface as a failed item.
    await ctx.main.execute(
        "INSERT INTO stage_error("
        "    stage, pipeline, error_type, message, retryable, occurred_at_ms"
        ") VALUES ('asr', 'audio', 'AuthError', 'config', 0, 50)",
    )
    # Current state: bvid_ok succeeded on retry; bvid_bad is still failed.
    await ctx.main.execute(
        "INSERT INTO audio_transcription("
        "    bvid, status, payload, processed_at_ms"
        ") VALUES (?, 'success', ?, ?)",
        (bvid_ok, "{}", 300),
    )
    await ctx.main.execute(
        "INSERT INTO audio_transcription("
        "    bvid, status, payload, processed_at_ms"
        ") VALUES (?, 'failed', ?, ?)",
        (bvid_bad, "{}", 300),
    )

    # The consumer's query: distinct item_ids from stage_error that are NOT
    # currently 'success' in audio_transcription, with item_id non-null.
    rows = await ctx.main.fetch_all(
        """
        SELECT DISTINCT e.item_id
          FROM stage_error e
          LEFT JOIN audio_transcription t
            ON t.bvid = e.item_id
         WHERE e.stage = 'asr'
           AND e.item_id IS NOT NULL
           AND (t.status IS NULL OR t.status <> 'success')
         ORDER BY e.item_id
        """,
    )
    failed_bvids = [r["item_id"] for r in rows]
    assert failed_bvids == [bvid_bad]
