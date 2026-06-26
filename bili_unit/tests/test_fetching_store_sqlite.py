# Contract tests for FetchingStore — the SQLite-backed write store that
# replaces fetching/data.py's DataStore + fetching/error.py's ErrorStore.
#
# These tests build their own UidContext on tmp_path and don't touch the old
# conftest 'stores' fixture (which still wraps the legacy file-directory
# implementation).

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from bili_unit._db import UidContext
from bili_unit.fetching._store import FetchingStore

UID = 42


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    ctx = UidContext(uid=UID, root=tmp_path)
    await ctx.open()
    s = FetchingStore(ctx)
    try:
        yield s
    finally:
        await ctx.close()


# ---------------------------------------------------------------------------
# raw DB writes
# ---------------------------------------------------------------------------


async def test_save_raw_payload_endpoint_level(store: FetchingStore) -> None:
    payload = {"list": {"vlist": [{"aid": 1}]}, "page": {"count": 1}}
    await store.save_raw_payload("user_info", "", payload)

    row = await store.ctx.conn.fetch_one(
        "SELECT endpoint, item_id, payload, fetched_at_ms FROM raw_payload WHERE endpoint = ? AND item_id = ?",
        ("user_info", ""),
    )
    assert row is not None
    assert row["endpoint"] == "user_info"
    assert row["item_id"] == ""
    assert json.loads(row["payload"]) == payload
    assert row["fetched_at_ms"] > 0


async def test_save_raw_payload_fanout_item(store: FetchingStore) -> None:
    payload = {"bvid": "BV1abc", "title": "demo"}
    await store.save_raw_payload(
        "video_detail",
        "BV1abc",
        payload,
        fetched_at_ms=12345,
    )

    row = await store.ctx.conn.fetch_one(
        "SELECT payload, fetched_at_ms FROM raw_payload WHERE endpoint = ? AND item_id = ?",
        ("video_detail", "BV1abc"),
    )
    assert row is not None
    assert json.loads(row["payload"]) == payload
    assert row["fetched_at_ms"] == 12345


async def test_save_raw_payload_upserts_on_conflict(store: FetchingStore) -> None:
    await store.save_raw_payload("user_info", "", {"v": 1}, fetched_at_ms=100)
    await store.save_raw_payload("user_info", "", {"v": 2}, fetched_at_ms=200)

    rows = await store.ctx.conn.fetch_all(
        "SELECT payload, fetched_at_ms FROM raw_payload WHERE endpoint = ?",
        ("user_info",),
    )
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == {"v": 2}
    assert rows[0]["fetched_at_ms"] == 200


async def test_save_raw_page_and_progress_atomic(store: FetchingStore) -> None:
    payload = {"pages": [{"pn": 1}]}
    progress = {"cursor": "next-token", "total": 100, "fetched": 30}
    await store.save_raw_page_and_progress(
        "videos",
        "",
        payload,
        progress,
        fetched_at_ms=500,
    )

    payload_row = await store.ctx.conn.fetch_one(
        "SELECT payload, fetched_at_ms FROM raw_payload WHERE endpoint = ? AND item_id = ?",
        ("videos", ""),
    )
    assert payload_row is not None
    assert json.loads(payload_row["payload"]) == payload
    assert payload_row["fetched_at_ms"] == 500

    prog_row = await store.ctx.conn.fetch_one(
        "SELECT cursor, total, fetched, updated_at_ms FROM fetch_progress WHERE endpoint = ?",
        ("videos",),
    )
    assert prog_row is not None
    assert prog_row["cursor"] == "next-token"
    assert prog_row["total"] == 100
    assert prog_row["fetched"] == 30
    assert prog_row["updated_at_ms"] == 500


async def test_save_raw_page_and_progress_dict_cursor(store: FetchingStore) -> None:
    """Dict cursor (e.g. next_request) is JSON-serialised on write."""
    progress = {"cursor": {"pn": 2, "ps": 30}, "total": None, "fetched": None}
    await store.save_raw_page_and_progress(
        "videos",
        "",
        {"x": 1},
        progress,
    )
    row = await store.ctx.conn.fetch_one(
        "SELECT cursor FROM fetch_progress WHERE endpoint = ?",
        ("videos",),
    )
    assert row is not None
    assert json.loads(row["cursor"]) == {"pn": 2, "ps": 30}


async def test_save_progress_alone(store: FetchingStore) -> None:
    await store.save_progress(
        "videos",
        {"cursor": None, "total": 50, "fetched": 50},
        updated_at_ms=999,
    )
    row = await store.ctx.conn.fetch_one(
        "SELECT cursor, total, fetched, updated_at_ms FROM fetch_progress WHERE endpoint = ?",
        ("videos",),
    )
    assert row is not None
    assert row["cursor"] is None
    assert row["total"] == 50
    assert row["fetched"] == 50
    assert row["updated_at_ms"] == 999


async def test_save_progress_upserts(store: FetchingStore) -> None:
    await store.save_progress("videos", {"cursor": "a", "total": 10, "fetched": 1})
    await store.save_progress("videos", {"cursor": "b", "total": 20, "fetched": 5})
    rows = await store.ctx.conn.fetch_all(
        "SELECT cursor, total, fetched FROM fetch_progress WHERE endpoint = ?",
        ("videos",),
    )
    assert len(rows) == 1
    assert rows[0]["cursor"] == "b"
    assert rows[0]["total"] == 20
    assert rows[0]["fetched"] == 5


# ---------------------------------------------------------------------------
# raw DB reads
# ---------------------------------------------------------------------------


async def test_get_raw_payload_round_trip(store: FetchingStore) -> None:
    payload = {"a": 1, "nested": {"b": [1, 2, 3]}}
    await store.save_raw_payload("user_info", "", payload)
    got = await store.get_raw_payload("user_info")
    assert got == payload


async def test_get_raw_payload_missing(store: FetchingStore) -> None:
    assert await store.get_raw_payload("missing_ep") is None
    assert await store.get_raw_payload("user_info", "BVXYZ") is None


async def test_get_raw_fetched_at_ms_round_trip(store: FetchingStore) -> None:
    await store.save_raw_payload("user_info", "", {"ok": True}, fetched_at_ms=111)
    await store.save_raw_payload(
        "video_detail",
        "BV1",
        {"ok": True},
        fetched_at_ms=222,
    )

    assert await store.get_raw_fetched_at_ms("user_info") == 111
    assert await store.get_raw_fetched_at_ms("video_detail", "BV1") == 222
    assert await store.get_raw_fetched_at_ms("missing") is None


async def test_get_progress_round_trip_string_cursor(store: FetchingStore) -> None:
    await store.save_progress(
        "videos",
        {"cursor": "tok-1", "total": 10, "fetched": 3},
    )
    got = await store.get_progress("videos")
    assert got is not None
    assert got["cursor"] == "tok-1"
    assert got["total"] == 10
    assert got["fetched"] == 3
    assert got["updated_at_ms"] > 0


async def test_get_progress_round_trip_dict_cursor(store: FetchingStore) -> None:
    await store.save_progress(
        "videos",
        {"cursor": {"pn": 3, "ps": 30}, "total": None, "fetched": None},
    )
    got = await store.get_progress("videos")
    assert got is not None
    assert got["cursor"] == {"pn": 3, "ps": 30}


async def test_get_progress_missing(store: FetchingStore) -> None:
    assert await store.get_progress("nothing") is None


async def test_list_completed_items_excludes_endpoint_level(
    store: FetchingStore,
) -> None:
    await store.save_raw_payload("video_detail", "", {"meta": True})
    await store.save_raw_payload("video_detail", "BV1aaa", {"id": 1})
    await store.save_raw_payload("video_detail", "BV1bbb", {"id": 2})
    await store.save_raw_payload("other_ep", "BV9999", {"id": 9})

    items = await store.list_completed_items("video_detail")
    assert items == ["BV1aaa", "BV1bbb"]
    other = await store.list_completed_items("other_ep")
    assert other == ["BV9999"]


async def test_list_completed_items_empty(store: FetchingStore) -> None:
    assert await store.list_completed_items("video_detail") == []


async def test_list_fanout_payloads_round_trip(store: FetchingStore) -> None:
    await store.save_raw_payload("video_detail", "BV1", {"id": 1, "title": "a"})
    await store.save_raw_payload("video_detail", "BV2", {"id": 2, "title": "b"})
    await store.save_raw_payload("video_detail", "", {"meta": True})

    out = await store.list_fanout_payloads("video_detail")
    assert out == {
        "BV1": {"id": 1, "title": "a"},
        "BV2": {"id": 2, "title": "b"},
    }


async def test_list_fanout_payloads_empty(store: FetchingStore) -> None:
    assert await store.list_fanout_payloads("video_detail") == {}


async def test_list_fanout_payload_records_round_trip(
    store: FetchingStore,
) -> None:
    await store.save_raw_payload(
        "video_detail",
        "BV1",
        {"id": 1},
        fetched_at_ms=1000,
    )
    await store.save_raw_payload(
        "video_detail",
        "BV2",
        {"id": 2},
        fetched_at_ms=2000,
    )
    await store.save_raw_payload(
        "video_detail",
        "",
        {"meta": True},
        fetched_at_ms=500,
    )

    assert await store.list_fanout_payload_records("video_detail") == {
        "BV1": {"payload": {"id": 1}, "fetched_at_ms": 1000},
        "BV2": {"payload": {"id": 2}, "fetched_at_ms": 2000},
    }


async def test_list_item_ages_ms_round_trip(store: FetchingStore) -> None:
    await store.save_raw_payload("video_detail", "BV1", {}, fetched_at_ms=1000)
    await store.save_raw_payload("video_detail", "BV2", {}, fetched_at_ms=2000)
    await store.save_raw_payload("video_detail", "", {}, fetched_at_ms=500)

    ages = await store.list_item_ages_ms("video_detail")
    assert ages == {"BV1": 1000, "BV2": 2000}


# ---------------------------------------------------------------------------
# main DB writes — task + endpoint state
# ---------------------------------------------------------------------------


async def test_init_task_seeds_task_and_endpoints(store: FetchingStore) -> None:
    await store.init_task(["user_info", "videos", "video_detail"])

    task_row = await store.ctx.conn.fetch_one(
        "SELECT stage, status, payload, created_at_ms, updated_at_ms FROM stage_task WHERE stage = ?",
        ("fetching",),
    )
    assert task_row is not None
    assert task_row["stage"] == "fetching"
    assert task_row["status"] == "PENDING"
    assert json.loads(task_row["payload"]) == {
        "endpoints": ["user_info", "videos", "video_detail"],
    }
    assert task_row["created_at_ms"] > 0
    assert task_row["updated_at_ms"] >= task_row["created_at_ms"]

    ep_rows = await store.ctx.conn.fetch_all(
        "SELECT endpoint, status, retry_count, last_error_id, "
        "       item_progress, progress, updated_at_ms "
        "FROM fetch_endpoint_state ORDER BY endpoint",
    )
    assert {r["endpoint"] for r in ep_rows} == {
        "user_info",
        "videos",
        "video_detail",
    }
    for row in ep_rows:
        assert row["status"] == "PENDING"
        assert row["retry_count"] == 0
        assert row["last_error_id"] is None
        assert row["item_progress"] is None
        assert row["progress"] is None


async def test_init_task_idempotent(store: FetchingStore) -> None:
    await store.init_task(["user_info"])
    # Mutate the endpoint state to something non-default …
    await store.update_endpoint_state(
        "user_info",
        status="SUCCESS",
        retry_count=2,
        item_progress={"total": 10, "completed": 10, "failed": 0},
    )
    await store.update_task_status("SUCCESS")

    # … then re-init: existing rows must be preserved.
    await store.init_task(["user_info", "videos"])

    task_status = await store.get_task_status()
    assert task_status == "SUCCESS"

    user_state = await store.get_endpoint_state("user_info")
    assert user_state is not None
    assert user_state["status"] == "SUCCESS"
    assert user_state["retry_count"] == 2
    assert user_state["item_progress"] == {
        "total": 10,
        "completed": 10,
        "failed": 0,
    }

    # newly added endpoint is created PENDING
    videos_state = await store.get_endpoint_state("videos")
    assert videos_state is not None
    assert videos_state["status"] == "PENDING"
    assert videos_state["retry_count"] == 0


async def test_update_task_status(store: FetchingStore) -> None:
    await store.init_task(["user_info"])
    assert await store.get_task_status() == "PENDING"
    await store.update_task_status("RUNNING")
    assert await store.get_task_status() == "RUNNING"
    await store.update_task_status("SUCCESS")
    assert await store.get_task_status() == "SUCCESS"


async def test_update_endpoint_state_inserts_when_missing(
    store: FetchingStore,
) -> None:
    """Upsert behaviour: works even without init_task (defensive insert)."""
    await store.update_endpoint_state(
        "user_info",
        status="RUNNING",
        retry_count=0,
    )
    state = await store.get_endpoint_state("user_info")
    assert state is not None
    assert state["status"] == "RUNNING"
    assert state["retry_count"] == 0


async def test_update_endpoint_state_updates_existing(
    store: FetchingStore,
) -> None:
    await store.init_task(["user_info"])
    await store.update_endpoint_state(
        "user_info",
        status="RUNNING",
        retry_count=1,
        last_error_id=42,
        item_progress={"total": 5, "completed": 1, "failed": 0},
        progress={"cursor": "abc", "done": False},
    )
    state = await store.get_endpoint_state("user_info")
    assert state is not None
    assert state["status"] == "RUNNING"
    assert state["retry_count"] == 1
    assert state["last_error_id"] == 42
    assert state["item_progress"] == {"total": 5, "completed": 1, "failed": 0}
    assert state["progress"] == {"cursor": "abc", "done": False}


async def test_update_endpoint_state_preserves_unset_fields(
    store: FetchingStore,
) -> None:
    """When only ``status`` is bumped, item_progress/progress/last_error_id stick."""
    await store.update_endpoint_state(
        "user_info",
        status="RUNNING",
        retry_count=1,
        last_error_id=7,
        item_progress={"total": 3, "completed": 1, "failed": 0},
        progress={"cursor": "p1"},
    )
    # Now only update status & retry_count — others must survive.
    await store.update_endpoint_state(
        "user_info",
        status="FAILED_RETRYABLE",
        retry_count=2,
    )
    state = await store.get_endpoint_state("user_info")
    assert state is not None
    assert state["status"] == "FAILED_RETRYABLE"
    assert state["retry_count"] == 2
    assert state["last_error_id"] == 7
    assert state["item_progress"] == {"total": 3, "completed": 1, "failed": 0}
    assert state["progress"] == {"cursor": "p1"}


# ---------------------------------------------------------------------------
# main DB reads
# ---------------------------------------------------------------------------


async def test_get_task_status_missing(store: FetchingStore) -> None:
    assert await store.get_task_status() is None


async def test_get_endpoint_status_missing(store: FetchingStore) -> None:
    assert await store.get_endpoint_status("never_seen") is None


async def test_get_endpoint_state_missing(store: FetchingStore) -> None:
    assert await store.get_endpoint_state("never_seen") is None


# ---------------------------------------------------------------------------
# error sink
# ---------------------------------------------------------------------------


async def test_record_error_returns_monotonic_ids(store: FetchingStore) -> None:
    id1 = await store.record_error(
        endpoint="user_info",
        error_type="Http412Error",
        message="rate limited",
        retryable=True,
    )
    id2 = await store.record_error(
        endpoint="videos",
        error_type="RequestError",
        message="boom",
        retryable=True,
        detail={"item_id": "BV1abc", "retry_count": 1},
    )
    id3 = await store.record_error(
        endpoint=None,
        error_type="AuthError",
        message="no creds",
        retryable=False,
    )
    assert id1 >= 1
    assert id2 == id1 + 1
    assert id3 == id2 + 1


async def test_record_error_persists_all_fields(store: FetchingStore) -> None:
    err_id = await store.record_error(
        endpoint="video_detail",
        error_type="ResourceUnavailableError",
        message="taken down",
        retryable=False,
        detail={"item_id": "BVxyz"},
        occurred_at_ms=12345,
    )
    row = await store.ctx.conn.fetch_one(
        "SELECT * FROM stage_error WHERE id = ?",
        (err_id,),
    )
    assert row is not None
    assert row["stage"] == "fetching"
    assert row["endpoint"] == "video_detail"
    assert row["error_type"] == "ResourceUnavailableError"
    assert row["message"] == "taken down"
    assert row["retryable"] == 0
    assert json.loads(row["detail"]) == {"item_id": "BVxyz"}
    assert row["occurred_at_ms"] == 12345


async def test_record_error_retryable_unknown_stored_as_null(
    store: FetchingStore,
) -> None:
    err_id = await store.record_error(
        endpoint="user_info",
        error_type="WeirdError",
        message="?",
        retryable=None,
    )
    row = await store.ctx.conn.fetch_one(
        "SELECT retryable FROM stage_error WHERE id = ?",
        (err_id,),
    )
    assert row is not None
    assert row["retryable"] is None


async def test_list_errors_unfiltered_returns_newest_first(
    store: FetchingStore,
) -> None:
    id_a = await store.record_error(
        endpoint="ep_a",
        error_type="E",
        message="a",
        retryable=True,
    )
    id_b = await store.record_error(
        endpoint="ep_b",
        error_type="E",
        message="b",
        retryable=False,
    )
    id_c = await store.record_error(
        endpoint=None,
        error_type="E",
        message="c",
        retryable=None,
    )

    errors = await store.list_errors()
    assert [e["id"] for e in errors] == [id_c, id_b, id_a]
    assert errors[0]["retryable"] is None
    assert errors[1]["retryable"] is False
    assert errors[2]["retryable"] is True


async def test_list_errors_filtered_by_endpoint(store: FetchingStore) -> None:
    await store.record_error(
        endpoint="ep_a",
        error_type="E",
        message="a1",
        retryable=True,
    )
    await store.record_error(
        endpoint="ep_b",
        error_type="E",
        message="b1",
        retryable=True,
    )
    await store.record_error(
        endpoint="ep_a",
        error_type="E",
        message="a2",
        retryable=False,
        detail={"x": 1},
    )

    out = await store.list_errors(endpoint="ep_a")
    assert [e["message"] for e in out] == ["a2", "a1"]
    assert all(e["endpoint"] == "ep_a" for e in out)
    assert out[0]["detail"] == {"x": 1}


# ---------------------------------------------------------------------------
# list_failed_items
# ---------------------------------------------------------------------------


async def test_list_failed_items_drops_already_succeeded(
    store: FetchingStore,
) -> None:
    """An item that errored once but later got a raw_payload row counts as
    succeeded — drop it from the failed set."""
    await store.record_error(
        endpoint="video_detail",
        error_type="Http412Error",
        message="boom",
        retryable=True,
        detail={"item_id": "BV_failed"},
    )
    await store.record_error(
        endpoint="video_detail",
        error_type="RequestError",
        message="boom",
        retryable=True,
        detail={"item_id": "BV_recovered"},
    )
    # BV_recovered later succeeded.
    await store.save_raw_payload("video_detail", "BV_recovered", {"ok": True})

    failed = await store.list_failed_items("video_detail")
    assert failed == ["BV_failed"]


async def test_list_failed_items_filters_other_endpoints(
    store: FetchingStore,
) -> None:
    await store.record_error(
        endpoint="video_detail",
        error_type="E",
        message="m",
        retryable=True,
        detail={"item_id": "BV_v"},
    )
    await store.record_error(
        endpoint="article_detail",
        error_type="E",
        message="m",
        retryable=True,
        detail={"item_id": "CV_a"},
    )
    assert await store.list_failed_items("video_detail") == ["BV_v"]
    assert await store.list_failed_items("article_detail") == ["CV_a"]


async def test_list_failed_items_empty_when_no_errors(
    store: FetchingStore,
) -> None:
    assert await store.list_failed_items("video_detail") == []


async def test_list_failed_items_dedupes_repeated_failures(
    store: FetchingStore,
) -> None:
    for _ in range(3):
        await store.record_error(
            endpoint="video_detail",
            error_type="E",
            message="m",
            retryable=True,
            detail={"item_id": "BV1"},
        )
    assert await store.list_failed_items("video_detail") == ["BV1"]


async def test_list_unavailable_items_only_returns_terminal_unavailable(
    store: FetchingStore,
) -> None:
    await store.record_error(
        endpoint="video_detail",
        error_type="ResourceUnavailableError",
        message="gone",
        retryable=False,
        detail={"item_id": "BV_gone"},
    )
    await store.record_error(
        endpoint="video_detail",
        error_type="Http412Error",
        message="too fast",
        retryable=False,
        detail={"item_id": "BV_retry_later"},
    )
    await store.record_error(
        endpoint="article_detail",
        error_type="ResourceUnavailableError",
        message="gone",
        retryable=False,
        detail={"item_id": "CV_other_endpoint"},
    )
    await store.record_error(
        endpoint="video_detail",
        error_type="ResourceUnavailableError",
        message="recovered",
        retryable=False,
        detail={"item_id": "BV_recovered"},
    )
    await store.save_raw_payload("video_detail", "BV_recovered", {"ok": True})

    assert await store.list_unavailable_items("video_detail") == ["BV_gone"]


# ---------------------------------------------------------------------------
# Misc / smoke
# ---------------------------------------------------------------------------


async def test_get_endpoint_status_after_state_writes(
    store: FetchingStore,
) -> None:
    await store.init_task(["user_info"])
    assert await store.get_endpoint_status("user_info") == "PENDING"
    await store.update_endpoint_state("user_info", status="RUNNING")
    assert await store.get_endpoint_status("user_info") == "RUNNING"
    await store.update_endpoint_state("user_info", status="SUCCESS")
    assert await store.get_endpoint_status("user_info") == "SUCCESS"


@pytest.mark.parametrize("item_id", ["", "BV1", "CV987", "opus_42"])
async def test_save_raw_payload_handles_various_item_ids(
    store: FetchingStore,
    item_id: str,
) -> None:
    await store.save_raw_payload("ep", item_id, {"i": item_id})
    got = await store.get_raw_payload("ep", item_id)
    assert got == {"i": item_id}
