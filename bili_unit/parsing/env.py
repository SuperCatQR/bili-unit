# env — read-only .env loader for parsing settings (pydantic-settings).

from pydantic_settings import BaseSettings, SettingsConfigDict


class ParsingEnv(BaseSettings):
    """Bilibili parsing settings, loaded lazily from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage path
    bili_parsing_data_dir: str = "data/bili/parsing"

    # Image downloading
    bili_parsing_image_concurrency: int = 8
    bili_parsing_image_timeout: float = 30.0


_settings: ParsingEnv | None = None


def get_parsing_settings() -> ParsingEnv:
    """Return the cached parsing settings, loading .env on first call."""
    global _settings
    if _settings is None:
        _settings = ParsingEnv()
    return _settings


def reload_parsing_settings() -> None:
    """Force reload from .env."""
    global _settings
    _settings = ParsingEnv()
