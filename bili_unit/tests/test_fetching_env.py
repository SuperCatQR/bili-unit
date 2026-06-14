# tests for bili_unit/_env (single source of truth for settings).
# Run: uv run pytest bili_unit/tests/test_fetching_env.py -v

import pytest

from bili_unit._env import BiliSettings, get_settings, reload_settings


@pytest.fixture(autouse=True)
def _clean_env_cache(monkeypatch):
    """Force fresh settings each test, isolated from real .env."""
    # Clear all BILI_* env vars so .env file values don't leak in
    for _key in list(monkeypatch._monkeypatches.keys() if hasattr(monkeypatch, '_monkeypatches') else []):
        pass
    import os
    for key in [k for k in os.environ if k.startswith("BILI_")]:
        monkeypatch.delenv(key, raising=False)
    # Point to a non-existent env file to prevent reading the real .env
    monkeypatch.setenv("BILI_SESSDATA", "")
    monkeypatch.setenv("BILI_JCT", "")
    monkeypatch.setenv("BILI_BUVID3", "")
    reload_settings()


def test_env_import_does_not_require_dotenv():
    """Importing env module does not crash when .env is missing."""
    settings = get_settings()
    assert isinstance(settings, BiliSettings)


def test_env_defaults():
    """All credential fields default to empty string."""
    s = get_settings()
    assert s.bili_sessdata == ""
    assert s.bili_jct == ""
    assert s.bili_buvid3 == ""


def test_env_reads_from_env_vars(monkeypatch):
    """Pydantic Settings loads from os.environ."""
    monkeypatch.setenv("BILI_SESSDATA", "abc")
    monkeypatch.setenv("BILI_JCT", "xyz")
    reload_settings()
    s = get_settings()
    assert s.bili_sessdata == "abc"
    assert s.bili_jct == "xyz"


def test_env_fetching_defaults():
    """Fetching config has expected defaults."""
    s = get_settings()
    assert s.bili_fetching_data_dir == "data/bili/fetching/data"
    assert s.bili_fetching_http_backend == "aiohttp"
    assert s.bili_fetching_global_qps == 1.0
    assert s.bili_fetching_max_retries == 3


def test_env_stale_running_threshold_default():
    """Default stale-running threshold is 15 minutes (issue #3)."""
    s = get_settings()
    assert s.bili_fetching_stale_running_threshold_seconds == 900
