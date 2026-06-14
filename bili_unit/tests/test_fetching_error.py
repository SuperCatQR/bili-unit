# tests for bili_unit/fetching/error
# Run: uv run pytest bili_unit/tests/test_error.py -v

from pathlib import Path

import pytest

from bili_unit.fetching import AuthError, Http412Error, RequestError
from bili_unit.fetching.error import ErrorStore


@pytest.mark.asyncio
async def test_error_store_record_and_list(tmp_path: Path):
    es = ErrorStore(str(tmp_path / "test_errors"))
    await es.open()
    try:
        eid = await es.record(
            Http412Error("too fast"), uid=99, endpoint="videos",
            retryable=True, detail={"page": 3},
        )
        assert eid == 1

        await es.record(
            AuthError("no sessdata"), uid=None, retryable=False,
        )

        errs = await es.list_errors()
        assert len(errs) == 2
        # retryable round-trips as bool (was a string in older releases).
        retryable_values = {e.retryable for e in errs}
        assert retryable_values == {True, False}

        errs_uid = await es.list_by_uid(99)
        # uid=NULL should NOT match uid=99 filter
        assert len(errs_uid) == 1
    finally:
        await es.close()


@pytest.mark.asyncio
async def test_error_store_normalises_legacy_string_retryable(tmp_path: Path):
    """Old releases wrote ``retryable`` as ``"true"``/``"false"``/``"unknown"``.

    Records on disk from those releases must still load — read coerces them
    back to ``True`` / ``False`` / ``None``.  Anything unrecognised collapses
    to ``None`` rather than raising.
    """
    import json

    base = tmp_path / "legacy_errors"
    base.mkdir()
    (base / "_counter.json").write_text(json.dumps({"next_id": 5}), encoding="utf-8")
    (base / "42.json").write_text(
        json.dumps([
            {"id": 1, "uid": 42, "endpoint": "videos",
             "error_type": "Http412Error", "message": "old retryable",
             "retryable": "true", "detail": None, "timestamp": 1},
            {"id": 2, "uid": 42, "endpoint": "videos",
             "error_type": "AuthError", "message": "old non-retryable",
             "retryable": "false", "detail": None, "timestamp": 2},
            {"id": 3, "uid": 42, "endpoint": "videos",
             "error_type": "RequestError", "message": "old unknown",
             "retryable": "unknown", "detail": None, "timestamp": 3},
            {"id": 4, "uid": 42, "endpoint": "videos",
             "error_type": "RequestError", "message": "garbage",
             "retryable": "yes please", "detail": None, "timestamp": 4},
        ]),
        encoding="utf-8",
    )

    es = ErrorStore(str(base))
    await es.open()
    try:
        errs = sorted(await es.list_by_uid(42), key=lambda e: e.id)
        assert [e.retryable for e in errs] == [True, False, None, None]
    finally:
        await es.close()


@pytest.mark.asyncio
async def test_error_store_delete_by_uid(tmp_path: Path):
    es = ErrorStore(str(tmp_path / "test_errors"))
    await es.open()
    try:
        # Record errors for two different uids plus one without uid
        await es.record(Http412Error("err1"), uid=100, endpoint="videos")
        await es.record(RequestError("err2"), uid=100, endpoint="user_info")
        await es.record(AuthError("err3"), uid=200, endpoint="videos")
        await es.record(AuthError("err4"), uid=None)

        # Delete uid=100
        deleted = await es.delete_by_uid(100)
        assert deleted == 2

        # Verify uid=100 errors are gone
        errs_100 = await es.list_by_uid(100)
        assert len(errs_100) == 0

        # Verify uid=200 and uid=NULL errors remain
        errs_all = await es.list_errors()
        assert len(errs_all) == 2

        # Deleting non-existent uid returns 0
        deleted2 = await es.delete_by_uid(999)
        assert deleted2 == 0
    finally:
        await es.close()
