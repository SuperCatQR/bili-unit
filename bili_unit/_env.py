# _env — single source of truth for bili_unit configuration.
#
# 三个 stage 各自的 env 模块（fetching/env、parsing/env、processing/env）
# 是 thin re-export 到此处。新代码请直接 import bili_unit._env，stage 级
# env 模块仅保留做向后兼容。
#
# 字段按 stage 前缀分组：
#   bili_*                 —— 凭据（跨 stage 用）
#   bili_fetching_*        —— fetching stage
#   bili_parsing_*         —— parsing stage
#   bili_processing_*      —— processing stage

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from ._retry import parse_retry_delays as _parse_retry_delays


class BiliSettings(BaseSettings):
    """Single configuration object loaded lazily from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === credentials (used by fetching, but not stage-prefixed) ===
    bili_sessdata: str = ""
    bili_jct: str = ""
    bili_buvid3: str = ""
    bili_buvid4: str = ""
    bili_dedeuserid: str = ""
    bili_ac_time_value: str = ""

    # === storage root (SQLite layout) ===
    # One uid maps to three locations under this root:
    #   {bili_db_dir}/{uid}.db        ← consumer contract  (parsing + processing tables, task state, errors)
    #   {bili_db_dir}/{uid}.raw.db    ← producer-private   (raw fetching payloads + cursor)
    #   {bili_db_dir}/{uid}/          ← workdir            (downloaded images, audio caches; DB stores rel paths)
    # See docs/refactor-plan-sqlite.md for the full layout rationale.
    bili_db_dir: str = "data/bili"

    # === fetching stage ===
    # Network / runner config. Storage paths are derived from bili_db_dir above.
    bili_fetching_http_backend: str = "aiohttp"
    bili_fetching_impersonate: str = "chrome131"
    bili_fetching_global_qps: float = 1.0  # was 0.5 (issue #2)
    bili_fetching_endpoint_qps: float = 0.5
    bili_fetching_request_timeout: float = 30.0
    bili_fetching_max_retries: int = 3
    bili_fetching_retry_delays: str = "30,60,120"
    bili_fetching_video_detail_qps: float = 0.5
    bili_fetching_recovery_cooldown: float = 300.0  # seconds before QPS starts recovering after 412
    bili_fetching_item_concurrency: int = 3  # max parallel item-level fan-out requests
    bili_fetching_refresh_after_days: float = 7.0  # refresh mode: re-fetch items older than N days
    bili_fetching_stale_running_threshold_seconds: int = 900  # issue #3: RUNNING task with updated_at older than this is treated as PARTIAL (process killed/timeout)

    # === parsing stage ===
    # Image downloading
    bili_parsing_image_concurrency: int = 8
    bili_parsing_image_timeout: float = 30.0

    # === processing stage ===
    # Storage path for ASR audio cache and ffmpeg temp dirs (these are large
    # binary blobs that don't belong in the SQLite databases).
    bili_processing_temp_dir: str = "data/bili/processing/temp"

    # Worker pools
    bili_processing_audio_workers: int = 2
    bili_processing_queue_maxsize: int = 16

    # Audio (MVP-后批次；MVP 不使用，但留字段以便配置)
    bili_processing_audio_quality: str = "64K"
    bili_processing_audio_max_segment_minutes: int = 8

    # ASR backend selection / config.
    #
    # Backend selection:
    #   mimo (default)  — MiMo cloud ASR via OpenAI-compatible chat completions.
    #   mock            — deterministic stub for tests / no-key environments.
    #   whisper         — reserved for future local backend.
    # CLI override: ``-b mock`` on the unified ``process`` command (see __main__).
    #
    # MiMo profile selects the base URL and is the recommended way to configure;
    # users do not have to memorise hosts.
    #     token_plan_cn   https://token-plan-cn.xiaomimimo.com/v1   (default)
    #     token_plan_sgp  https://token-plan-sgp.xiaomimimo.com/v1
    #     token_plan_ams  https://token-plan-ams.xiaomimimo.com/v1
    #     pay_as_you_go   https://api.xiaomimimo.com/v1             (sk-* keys)
    #     custom          use BILI_PROCESSING_ASR_BASE_URL verbatim (relays / self-hosted)
    #
    # Token Plan keys (tp-*) and pay-as-you-go keys (sk-*) are NOT interchangeable
    # per MiMo docs. Relays usually expect ``Authorization: Bearer``; set
    # BILI_PROCESSING_ASR_AUTH_STYLE=bearer for those (default ``api_key`` works
    # for both official endpoints).
    #
    # ASR endpoint reuses the OpenAI-compatible chat completions path:
    #     POST {BASE_URL}/chat/completions  with model="mimo-v2.5-asr"
    bili_processing_asr_backend: str = "mimo"
    bili_processing_asr_profile: str = "token_plan_cn"
    bili_processing_asr_auth_style: str = "api_key"  # api_key | bearer
    bili_processing_asr_api_key: str = ""
    bili_processing_asr_base_url: str = ""  # only consulted when profile="custom"
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

    # VAD-aware segmentation (improves transcript quality on long clips).
    #
    # When the token-budget path triggers (clip exceeds the 5400-token cap),
    # we run Silero VAD (ONNX, via ``pysilero-vad`` — no torch dependency)
    # to find silence gaps and cut at those gaps instead of fixed seconds.
    # This avoids splitting words / sentences mid-flow, which the ASR cannot
    # recover from once it sees an incomplete leading utterance.
    #
    # Disable with BILI_PROCESSING_ASR_USE_VAD=false to fall back to
    # fixed-period segmentation (the old behaviour); useful when the VAD
    # model can't be loaded (no onnxruntime, etc.) or for A/B testing.
    #
    # Threshold rationale:
    #   - 0.3 (default) is more sensitive than upstream's 0.5 default,
    #     chosen to better detect softer / mixed-music speech in B站 content.
    #   - Increase if too many gaps are missed (BGM mistaken for speech →
    #     no silence to cut at → forced overlap hard-cut).
    #   - Decrease if cuts are landing in audible speech.
    bili_processing_asr_use_vad: bool = True
    bili_processing_asr_vad_threshold: float = 0.3
    bili_processing_asr_vad_min_silence_sec: float = 0.4
    bili_processing_asr_vad_min_speech_sec: float = 0.2
    bili_processing_asr_vad_min_seg_sec: float = 60.0
    bili_processing_asr_vad_overlap_sec: float = 2.5

    # ASR resume-from-failure cache.
    #
    # Each successful per-segment ASR call is cached on disk keyed by the
    # segment's source-timeline ``(start_s, end_s)``.  When a bvid retries
    # (network blip, quota exhaustion, process killed), already-transcribed
    # segments are re-used and only the missing ones hit the API.  The
    # cache is cleared when the bvid completes successfully — a healthy
    # cache directory only contains in-flight or recently-failed work.
    #
    # Disable for debugging / one-shot runs that should always re-bill.
    bili_processing_asr_cache_enabled: bool = True
    bili_processing_asr_cache_dir: str = "data/bili/processing/asr_cache"

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

    def get_fetching_retry_delays(self) -> list[int]:
        """Parse ``bili_fetching_retry_delays`` into a sorted list of seconds."""
        return _parse_retry_delays(self.bili_fetching_retry_delays)

    def get_processing_retry_delays(self) -> list[int]:
        """Parse ``bili_processing_retry_delays`` into a sorted list of seconds."""
        return _parse_retry_delays(self.bili_processing_retry_delays)


# Singleton — lazy-loaded on first call.
_settings: BiliSettings | None = None


def get_settings() -> BiliSettings:
    """Return the cached settings, loading .env on first call."""
    global _settings
    if _settings is None:
        _settings = BiliSettings()
    return _settings


def reload_settings() -> None:
    """Force reload settings from .env (e.g. after user updates configuration)."""
    global _settings
    _settings = BiliSettings()
