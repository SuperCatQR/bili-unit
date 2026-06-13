# shared fixtures for bili_unit/fetching tests.

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from bilibili_api import Credential

from bili_unit.fetching.client import FetchPageResult
from bili_unit.fetching.command import Command
from bili_unit.fetching.data import DataStore
from bili_unit.fetching.error import ErrorStore
from bili_unit.fetching.query import Query
from bili_unit.fetching.rate_limit import RateLimitController
from bili_unit.fetching.runner import Runner

# fake credential for auth-free tests
_FAKE_CRED = Credential(sessdata="test", bili_jct="test", buvid3="test")


# 全局屏蔽真实 sleep，防止 retry 延迟（30/60/120s）拖慢测试套件。
# 局部 patch（如 test_retry.py 里的 side_effect=fake_sleep）会临时覆盖此 fixture，行为不变。
@pytest.fixture(autouse=True)
def _mock_retry_sleep():
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        yield


@pytest_asyncio.fixture(autouse=True)
async def _mock_get_credential():
    """All integration tests run without real .env."""
    with patch(
        "bili_unit.fetching.runner.get_credential",
        new=AsyncMock(return_value=_FAKE_CRED),
    ):
        yield


@pytest_asyncio.fixture
async def stores(tmp_path: Path):
    ds = DataStore(str(tmp_path / "data"))
    es = ErrorStore(str(tmp_path / "errors"))
    await ds.open()
    await es.open()
    yield ds, es
    await ds.close()
    await es.close()


@pytest_asyncio.fixture
async def rl_ctl():
    return RateLimitController(global_qps=10.0, endpoint_qps=10.0, pause_seconds=0)


@pytest_asyncio.fixture
async def runner(stores, rl_ctl):
    ds, es = stores
    return Runner(ds, es, rl_ctl)


@pytest_asyncio.fixture
async def command(stores, rl_ctl):
    ds, es = stores
    return Command(ds, es, rl_ctl)


@pytest_asyncio.fixture
async def query(stores):
    ds, es = stores
    return Query(ds, es)


# helpers

def _fake_page(uid: int, data: dict, is_last: bool = True, next_req: dict | None = None):
    """Return a FetchPageResult mimicking a successful API call."""
    return FetchPageResult(
        uid=uid, endpoint="user_info",
        raw_payload=data, is_last_page=is_last, next_request=next_req,
    )


def _fake_videos_pages(uid: int, total_pages: int = 3):
    """Generator that yields successive video pages, then empty last page."""
    for pn in range(1, total_pages):
        yield FetchPageResult(
            uid=uid, endpoint="videos",
            raw_payload={"list": {"vlist": [{"aid": pn * 100 + i} for i in range(30)]}, "page": {"count": 65}},
            is_last_page=False, next_request={"pn": pn + 1, "ps": 30},
        )
    yield FetchPageResult(
        uid=uid, endpoint="videos",
        raw_payload={"list": {"vlist": [{"aid": 999}]}, "page": {"count": 65}},
        is_last_page=True, next_request=None,
    )
