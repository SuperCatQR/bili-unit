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
            retryable="true", detail={"page": 3},
        )
        assert eid == 1

        await es.record(
            AuthError("no sessdata"), uid=None, retryable="false",
        )

        errs = await es.list_errors()
        assert len(errs) == 2

        errs_uid = await es.list_by_uid(99)
        # uid=NULL should NOT match uid=99 filter
        assert len(errs_uid) == 1
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
