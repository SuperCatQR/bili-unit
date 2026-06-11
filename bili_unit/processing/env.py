# env — read-only .env loader for processing settings (pydantic-settings).

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_retry_delays(raw: str) -> list[int]:
    """Parse comma-separated delay string into sorted list of seconds."""
    try:
        delays = [int(s.strip()) for s in raw.split(",") if s.strip()]
    except ValueError:
        delays = [30, 60, 120]
    return sorted(delays) if delays else [30]


class ProcessingEnv(BaseSettings):
    """Bilibili processing settings, loaded lazily from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage paths
    bili_processing_data_dir: str = "data/bili/processing/data"
    bili_processing_temp_dir: str = "data/bili/processing/temp"
    bili_processing_error_dir: str = "data/bili/processing/error"

    # Worker pools
    bili_processing_transform_workers: int = 4
    bili_processing_audio_workers: int = 2
    bili_processing_queue_maxsize: int = 16

    # Audio (MVP-后批次；MVP 不使用，但留字段以便配置)
    bili_processing_audio_quality: str = "64K"
    bili_processing_audio_max_segment_minutes: int = 8

    # ASR backend selection / config.
    #
    # MiMo Token Plan keys (tp-*) must use the regional Token Plan host:
    #     https://token-plan-cn.xiaomimimo.com/v1   (默认)
    #     https://token-plan-sgp.xiaomimimo.com/v1
    #     https://token-plan-ams.xiaomimimo.com/v1
    # Pay-as-you-go keys (sk-*) use https://api.xiaomimimo.com/v1 instead.
    # ASR endpoint reuses the OpenAI-compatible chat completions path:
    #     POST {BASE_URL}/chat/completions  with model="mimo-v2.5-asr"
    bili_processing_asr_backend: str = "mock"  # MVP 默认 mock；后续支持 mimo / whisper
    bili_processing_asr_api_key: str = ""
    bili_processing_asr_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    bili_processing_asr_model: str = "mimo-v2.5-asr"
    bili_processing_asr_language: str = "auto"
    bili_processing_asr_timeout: int = 300
    bili_processing_asr_max_file_size_mb: int = 10

    # Token-budget segmentation (root cause for 8192-token MiMo errors).
    #
    # MiMo mimo-v2.5-asr has an 8192-token context window.  Audio costs roughly
    # ~6.5 tokens/sec at 16 kHz mono (measured from real responses:
    # 134 s audio → 837 audio_tokens; failed 1033 s clip → 6502 tokens).
    # A single long clip easily exceeds the budget *even though its file size
    # is well under 10 MB* — so size-based segmentation alone does not protect.
    #
    # Strategy: when a clip's estimated input tokens
    #   ceil(duration_s * tokens_per_second) + reserved overhead
    # exceeds ``asr_max_input_tokens``, segment to the longest length that
    # still fits.  Size threshold remains as a fallback for clips with no
    # known duration.
    bili_processing_asr_max_input_tokens: int = 5400
    bili_processing_asr_tokens_per_second: float = 6.5
    bili_processing_asr_max_completion_tokens: int = 1024

    # Retry (per-work-item, within a single process_uid run).
    #
    # Follows the same design philosophy as fetching retry:
    #   max_retries  — how many times a failed item is retried before giving up.
    #   retry_delays — list of delay seconds between retries (exponential-ish).
    #                  If retry_count exceeds the list length, the last value is reused.
    #
    # Defaults mirror fetching ([30, 60, 120], max_retries=3) but are kept
    # separate so processing can be tuned independently.
    bili_processing_max_retries: int = 3
    bili_processing_retry_delays: str = "30,60,120"

    # External tools.
    # bili_processing_ffmpeg_path:
    #   "auto"     prefer system ffmpeg, fall back to imageio-ffmpeg (recommended)
    #   "system"   require system ffmpeg
    #   "imageio"  require imageio-ffmpeg bundled binary
    #   any other  treated as an explicit path.
    bili_processing_ffmpeg_path: str = "auto"

    def get_retry_delays(self) -> list[int]:
        """Parse ``bili_processing_retry_delays`` into a sorted list of seconds."""
        return _parse_retry_delays(self.bili_processing_retry_delays)


# Singleton — lazy-loaded on first call.
_settings: ProcessingEnv | None = None


def get_processing_settings() -> ProcessingEnv:
    """Return the cached processing settings, loading .env on first call."""
    global _settings
    if _settings is None:
        _settings = ProcessingEnv()
    return _settings


def reload_processing_settings() -> None:
    """Force reload from .env (e.g. after user updates configuration)."""
    global _settings
    _settings = ProcessingEnv()
