# bili_unit.processing.audio._init_wizard — interactive setup for MiMo ASR.
#
# Invoked by ``python -m bili_unit init-mimo``. Asks the user which MiMo
# profile / cluster they want, an API key, and (for the custom profile) a
# base URL + auth_style; appends the resulting BILI_PROCESSING_ASR_* keys
# to .env (overwriting any existing ones with the same name).
#
# Pure stdin/stdout — no fancy TUI deps. Works over SSH and on Windows
# Terminal alike. Non-interactive callers (CI / scripts) should set the
# .env values directly instead of going through this wizard.

from __future__ import annotations

import logging
import math
import struct
import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from ..._env import BiliSettings, get_settings
from ._asr_backend import ASRBackend, ASRResult
from ._mimo_backend import PROFILE_BASE_URLS, create_mimo_backend

logger = logging.getLogger("bili.processing.audio.init_wizard")


# Ordered for menu display — first 3 are Token Plan clusters, then
# pay-as-you-go, then custom (relays / self-hosted).
PROFILE_CHOICES: list[tuple[str, str]] = [
    ("token_plan_cn",  "Token Plan / 中国集群    (tp-* keys)"),
    ("token_plan_sgp", "Token Plan / 新加坡集群  (tp-* keys)"),
    ("token_plan_ams", "Token Plan / 欧洲集群    (tp-* keys)"),
    ("pay_as_you_go",  "按量付费 / Pay-as-you-go  (sk-* keys)"),
    ("custom",         "自定义 / 中转站 / 自建    (BASE_URL 自填)"),
]

_FIELDS = (
    "BILI_PROCESSING_ASR_BACKEND",
    "BILI_PROCESSING_ASR_PROFILE",
    "BILI_PROCESSING_ASR_API_KEY",
    "BILI_PROCESSING_ASR_BASE_URL",
    "BILI_PROCESSING_ASR_AUTH_STYLE",
)


def _prompt(
    msg: str,
    *,
    default: str = "",
    reader: Callable[[str], str] = input,
) -> str:
    """Read a line, return stripped value or default when empty."""
    raw = reader(msg).strip()
    return raw or default


def _ask_profile(reader: Callable[[str], str] = input) -> str:
    print("\n请选择 MiMo ASR 后端模式：\n")
    for i, (_, label) in enumerate(PROFILE_CHOICES, start=1):
        print(f"  {i}) {label}")
    while True:
        raw = _prompt("\n输入序号 [1-5]，回车默认 1: ", default="1", reader=reader)
        if raw.isdigit() and 1 <= int(raw) <= len(PROFILE_CHOICES):
            return PROFILE_CHOICES[int(raw) - 1][0]
        print(f"  无效输入: {raw!r}；请输入 1-{len(PROFILE_CHOICES)} 之间的数字。")


def _ask_auth_style(reader: Callable[[str], str] = input) -> str:
    print(
        "\n鉴权方式（中转站通常用 bearer，官方端点两种都支持，默认 api_key）：",
    )
    print("  1) api_key    （header: api-key: $KEY，默认）")
    print("  2) bearer     （header: Authorization: Bearer $KEY）")
    while True:
        raw = _prompt("输入序号 [1-2]，回车默认 1: ", default="1", reader=reader)
        if raw == "1":
            return "api_key"
        if raw == "2":
            return "bearer"
        print(f"  无效输入: {raw!r}；请输入 1 或 2。")


def collect_config(
    *,
    reader: Callable[[str], str] = input,
) -> dict[str, str]:
    """Run the interactive flow; return a dict of .env field → value.

    Pure logic — does not touch the filesystem. ``reader`` is dependency-
    injected so tests can drive the wizard with a scripted iterator.
    """
    profile = _ask_profile(reader=reader)
    fields: dict[str, str] = {
        "BILI_PROCESSING_ASR_BACKEND": "mimo",
        "BILI_PROCESSING_ASR_PROFILE": profile,
    }

    if profile == "custom":
        while True:
            base_url = _prompt(
                "\n请输入 BASE_URL (例如 https://relay.example.com/v1): ",
                reader=reader,
            )
            if base_url:
                fields["BILI_PROCESSING_ASR_BASE_URL"] = base_url.rstrip("/")
                break
            print("  custom 模式必须填 BASE_URL。")
        fields["BILI_PROCESSING_ASR_AUTH_STYLE"] = _ask_auth_style(reader=reader)
    else:
        # Preset profile resolves base_url internally; clear any stale custom value.
        fields["BILI_PROCESSING_ASR_BASE_URL"] = ""
        # Official endpoints accept api-key by default — keep it simple unless
        # the user has a reason to prefer bearer.
        fields["BILI_PROCESSING_ASR_AUTH_STYLE"] = "api_key"

    while True:
        api_key = _prompt(
            "\n请输入 API Key (tp-* / sk-* / 中转站发的 key): ", reader=reader,
        )
        if api_key:
            fields["BILI_PROCESSING_ASR_API_KEY"] = api_key
            break
        print("  API Key 不能为空。")

    return fields


def write_env(
    fields: dict[str, str], env_path: str | Path = ".env",
) -> Path:
    """Append / overwrite BILI_PROCESSING_ASR_* keys in *env_path*.

    Mirrors the layout used by ``bili_unit.fetching.auth.save_credential_to_env``:
    rewrites lines with the same key, leaves all others (comments, fetching
    creds) intact, creates the file if absent.
    """
    path = Path(env_path)

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    # Drop any pre-existing BILI_PROCESSING_ASR_* lines that we manage,
    # leave everything else (including unmanaged ASR_* keys like
    # ASR_LANGUAGE / ASR_TIMEOUT) untouched.
    managed = set(_FIELDS)
    new_lines = [
        line for line in existing_lines
        if not any(line.startswith(f"{k}=") for k in managed)
    ]

    for key in _FIELDS:
        new_lines.append(f"{key}={fields.get(key, '')}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info("MiMo ASR config saved to %s", path)
    return path


def build_probe_wav(*, seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Return a tiny 16 kHz mono WAV used by ``init-mimo --test``."""
    sample_count = max(1, int(seconds * sample_rate))
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(sample_count):
            # Low-amplitude 440 Hz tone is short, valid audio, and cheap to bill.
            sample = int(1200 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames.extend(struct.pack("<h", sample))
        wav.writeframes(bytes(frames))
    return buf.getvalue()


async def probe_mimo_model(
    *,
    settings: BiliSettings | None = None,
    backend_factory: Callable[[BiliSettings], ASRBackend] = create_mimo_backend,
) -> ASRResult:
    """Call the configured MiMo backend once with a tiny WAV probe."""
    active_settings = settings or get_settings()
    backend = backend_factory(active_settings)
    try:
        return await backend.transcribe(
            build_probe_wav(),
            mime_type="audio/wav",
            language=active_settings.bili_processing_asr_language,
        )
    finally:
        await backend.close()


def run_wizard(env_path: str | Path = ".env") -> Path:
    """High-level entry: collect config interactively and write to .env."""
    print("=== bili_unit · MiMo ASR 后端配置向导 ===")
    fields = collect_config()
    path = write_env(fields, env_path=env_path)

    print(f"\n已写入 {path}：")
    for key in _FIELDS:
        value = fields.get(key, "")
        if key == "BILI_PROCESSING_ASR_API_KEY" and value:
            shown = value[:4] + "***" + value[-2:] if len(value) > 6 else "***"
        else:
            shown = value
        print(f"  {key}={shown}")

    profile = fields.get("BILI_PROCESSING_ASR_PROFILE", "")
    if profile in PROFILE_BASE_URLS:
        print(f"\n该 profile 解析后的 BASE_URL: {PROFILE_BASE_URLS[profile]}")
    elif profile == "custom":
        print(f"\ncustom BASE_URL: {fields.get('BILI_PROCESSING_ASR_BASE_URL', '')}")

    return path
