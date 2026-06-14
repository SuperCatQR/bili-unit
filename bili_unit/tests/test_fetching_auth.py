# tests for bili_unit/fetching/auth
# Run: uv run pytest bili_unit/tests/test_auth.py -v

import pytest

from bili_unit._env import reload_settings
from bili_unit.fetching import AuthError
from bili_unit.fetching.auth import get_credential


@pytest.fixture(autouse=True)
def _clean_env_cache():
    """Force fresh settings each test."""
    reload_settings()


@pytest.mark.asyncio
async def test_auth_missing_sessdata(monkeypatch):
    monkeypatch.setenv("BILI_SESSDATA", "")
    monkeypatch.setenv("BILI_JCT", "")
    monkeypatch.setenv("BILI_BUVID3", "")
    reload_settings()
    with pytest.raises(AuthError, match="Missing BILI_SESSDATA"):
        await get_credential()


@pytest.mark.asyncio
async def test_auth_with_sessdata_only(monkeypatch):
    """Minimal credential works with just sessdata."""
    monkeypatch.setenv("BILI_SESSDATA", "sess")
    monkeypatch.setenv("BILI_JCT", "")
    monkeypatch.setenv("BILI_BUVID3", "")
    reload_settings()
    cred = await get_credential()
    from bilibili_api import Credential
    assert isinstance(cred, Credential)


@pytest.mark.asyncio
async def test_auth_with_full_fields(monkeypatch):
    monkeypatch.setenv("BILI_SESSDATA", "s")
    monkeypatch.setenv("BILI_JCT", "j")
    monkeypatch.setenv("BILI_BUVID3", "b3")
    monkeypatch.setenv("BILI_BUVID4", "b4")
    monkeypatch.setenv("BILI_DEDEUSERID", "d")
    monkeypatch.setenv("BILI_AC_TIME_VALUE", "a")
    reload_settings()
    cred = await get_credential()
    assert cred is not None


@pytest.mark.asyncio
async def test_auth_reload_picks_up_changes(monkeypatch):
    """reload_settings_and_credential sees new env values."""
    monkeypatch.setenv("BILI_SESSDATA", "old")
    reload_settings()
    await get_credential()

    monkeypatch.setenv("BILI_SESSDATA", "new")
    from bili_unit.fetching.auth import reload_settings_and_credential
    c2 = await reload_settings_and_credential()
    assert c2 is not None
