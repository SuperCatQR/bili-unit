# shared fixtures for bili_unit tests.
#
# After Phase 6 every test file targets the SQLite stores; the legacy
# (ds, es) tuple fixture is gone for good. What still lives here:
#   * pytest-asyncio loop policy (default_loop_scope = 'function' — set in pyproject)
#   * a global retry-sleep mock (keeps retry-bearing tests fast)
#   * a global credential mock (so no .env is needed)

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from bilibili_api import Credential

from bili_unit._env import BiliSettings  # noqa: F401 — exposed for downstream tests via import

# ---------------------------------------------------------------------------
# Global mocks (used by every fetching/parsing/processing test that doesn't
# need an authenticated request — auth flow, retry timing, etc.)
# ---------------------------------------------------------------------------

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
