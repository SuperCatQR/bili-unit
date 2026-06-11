# env — read-only .env loader via pydantic-settings.

from pydantic_settings import BaseSettings, SettingsConfigDict


class BiliEnv(BaseSettings):
    """Bilibili credential settings, loaded from .env (lazy)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # credential fields
    bili_sessdata: str = ""
    bili_jct: str = ""
    bili_buvid3: str = ""
    bili_buvid4: str = ""
    bili_dedeuserid: str = ""
    bili_ac_time_value: str = ""

    # fetching config (engineering doc §14)
    bili_fetching_data_dir: str = "data/bili/fetching/data"
    bili_fetching_error_dir: str = "data/bili/fetching/error"
    bili_fetching_http_backend: str = "aiohttp"
    bili_fetching_impersonate: str = "chrome131"
    bili_fetching_global_qps: float = 0.5
    bili_fetching_endpoint_qps: float = 0.2
    bili_fetching_request_timeout: float = 30.0
    bili_fetching_max_retries: int = 3
    bili_fetching_video_detail_qps: float = 0.2
    bili_fetching_recovery_cooldown: float = 300.0  # seconds before QPS starts recovering after 412
    bili_fetching_item_concurrency: int = 3  # max parallel item-level fan-out requests
    bili_fetching_refresh_after_days: float = 7.0  # refresh mode: re-fetch items older than N days


# Singleton — lazy-loaded on first call.
_settings: BiliEnv | None = None


def get_settings() -> BiliEnv:
    """Return the cached settings, loading .env on first call."""
    global _settings
    if _settings is None:
        _settings = BiliEnv()
    return _settings


def reload_settings() -> None:
    """Force reload settings from .env (e.g. after user updates credentials)."""
    global _settings
    _settings = BiliEnv()
