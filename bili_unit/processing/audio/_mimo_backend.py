# audio/_mimo_backend — MiMo ASR backend via aiohttp.
#
#   POST {BASE_URL}/chat/completions with model=mimo-v2.5-asr.
#   Uses OpenAI-compatible ``input_audio`` content part with base64 data URI.
#
# Profiles (resolved via BiliSettings.bili_processing_asr_profile):
#   token_plan_cn   https://token-plan-cn.xiaomimimo.com/v1   (Token Plan, tp-* keys)
#   token_plan_sgp  https://token-plan-sgp.xiaomimimo.com/v1
#   token_plan_ams  https://token-plan-ams.xiaomimimo.com/v1
#   pay_as_you_go   https://api.xiaomimimo.com/v1             (按量付费, sk-* keys)
#   custom          BILI_PROCESSING_ASR_BASE_URL is required (relays / self-hosted)
#
# Token Plan keys (tp-*) and pay-as-you-go keys (sk-*) are NOT interchangeable
# per MiMo docs. Auth header defaults to ``api-key`` (works for both official
# endpoints); set ``auth_style="bearer"`` for relays that require
# ``Authorization: Bearer``.

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import aiohttp

from .. import ASRAPIError, ASRConfigError, ASRConnectionError, EmptyTranscriptError
from ._asr_backend import ASRResult

if TYPE_CHECKING:
    from ..._env import BiliSettings

logger = logging.getLogger("bili.processing.audio.mimo")


# Profile → base URL mapping.  Single source of truth; users do not memorise
# hosts — they pick a profile name from BILI_PROCESSING_ASR_PROFILE.
PROFILE_BASE_URLS: dict[str, str] = {
    "token_plan_cn":  "https://token-plan-cn.xiaomimimo.com/v1",
    "token_plan_sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
    "token_plan_ams": "https://token-plan-ams.xiaomimimo.com/v1",
    "pay_as_you_go":  "https://api.xiaomimimo.com/v1",
}

# auth_style values accepted on the wire.
_AUTH_STYLES = ("api_key", "bearer")

_REFUSAL_MARKERS = (
    "the request was rejected because it was considered high risk",
)


def resolve_base_url(profile: str, custom_base_url: str = "") -> str:
    """Resolve a profile name to its base URL.

    Raises :class:`ASRConfigError` on unknown profile or missing base_url
    when profile=='custom'.
    """
    profile = (profile or "").strip().lower()
    if profile == "custom":
        url = (custom_base_url or "").strip()
        if not url:
            raise ASRConfigError(
                "ASR profile 'custom' requires BILI_PROCESSING_ASR_BASE_URL "
                "to be set (run `python -m bili_unit init-mimo` to configure).",
            )
        return url.rstrip("/")
    if profile in PROFILE_BASE_URLS:
        return PROFILE_BASE_URLS[profile]
    raise ASRConfigError(
        f"Unknown ASR profile {profile!r}; expected one of "
        f"{list(PROFILE_BASE_URLS.keys()) + ['custom']}. "
        f"Run `python -m bili_unit init-mimo` to (re)configure.",
    )


class MimoASRBackend:
    """MiMo cloud ASR backend (mimo-v2.5-asr).

    Uses ``aiohttp.ClientSession`` against the OpenAI-compatible chat
    completions endpoint.  Maintains a single session for connection pooling;
    call :meth:`close` when done.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://token-plan-cn.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-asr",
        timeout: int = 300,
        max_completion_tokens: int | None = None,
        auth_style: str = "api_key",
    ) -> None:
        if auth_style not in _AUTH_STYLES:
            raise ASRConfigError(
                f"Unknown auth_style {auth_style!r}; expected one of {_AUTH_STYLES}.",
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_completion_tokens = max_completion_tokens
        self._auth_style = auth_style
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def auth_style(self) -> str:
        return self._auth_style

    @property
    def cache_namespace(self) -> str:
        return f"mimo:{self._base_url}:{self._model}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_style == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["api-key"] = self._api_key
        return headers

    async def transcribe(
        self,
        audio_data: bytes,
        mime_type: str = "audio/mp3",
        language: str = "auto",
    ) -> ASRResult:
        """Transcribe audio via MiMo ASR API.

        Builds the ``input_audio`` content part with a base64 data URI
        and maps the response to :class:`ASRResult`.
        """
        if not self._api_key:
            raise ASRConfigError(
                "MiMo ASR API key is empty. Run "
                "`python -m bili_unit init-mimo` to configure, or set "
                "BILI_PROCESSING_ASR_API_KEY in .env.",
            )

        session = await self._get_session()
        b64 = base64.b64encode(audio_data).decode("ascii")

        payload: dict = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{mime_type};base64,{b64}",
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": language},
        }
        # Cap completion tokens when configured.  Default OpenAI-style is
        # 2048 which steals headroom from the 8192-token context for long
        # audio inputs.  Setting it lower (e.g. 1024) buys ~1024 more tokens
        # for input audio.
        if self._max_completion_tokens is not None:
            payload["max_tokens"] = self._max_completion_tokens
        headers = self._build_headers()

        url = f"{self._base_url}/chat/completions"
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise ASRAPIError(
                        f"MiMo ASR returned {resp.status}: "
                        f"{body.get('error', body)}"
                    )
        except TimeoutError:
            raise ASRConnectionError(
                f"MiMo ASR request timed out after {self._timeout.total}s"
            ) from None
        except aiohttp.ClientError as exc:
            raise ASRConnectionError(
                f"MiMo ASR connection error: {exc}",
            ) from exc

        return self._parse_response(body, language)

    @staticmethod
    def _parse_response(body: dict, language: str) -> ASRResult:
        """Map MiMo response to ASRResult.

        Key fields (confirmed by real probe):
          - ``choices[0].message.content`` — full transcription text
          - ``usage.seconds`` — audio duration (integer, rounded up)
          - ``usage.prompt_tokens_details.audio_tokens`` — billable audio tokens
        """
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ASRAPIError(
                f"unexpected MiMo response shape: {exc}"
            ) from exc
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if finish_reason == "length":
            raise ASRAPIError(
                "MiMo ASR response was truncated by max_tokens; reduce "
                "BILI_PROCESSING_ASR_MAX_SEGMENT_SECONDS or raise "
                "BILI_PROCESSING_ASR_MAX_COMPLETION_TOKENS."
            )
        if not isinstance(text, str):
            raise ASRAPIError("unexpected MiMo response shape: content is not text")
        text = text.strip()
        if not text:
            raise EmptyTranscriptError(
                "MiMo ASR returned empty transcription text; inspect the video "
                "manually (it may have no speech, or the backend response may be abnormal)."
            )
        text_lower = text.lower()
        if any(marker in text_lower for marker in _REFUSAL_MARKERS):
            raise ASRAPIError(f"MiMo ASR rejected the request: {text.strip()}")

        usage = body.get("usage", {}) if isinstance(body.get("usage"), dict) else {}
        duration_raw = usage.get("seconds")
        duration: float | None = (
            float(duration_raw) if duration_raw is not None else None
        )

        prompt_details = usage.get("prompt_tokens_details", {})
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        audio_tokens_raw = prompt_details.get("audio_tokens")
        try:
            audio_tokens: int | None = (
                int(audio_tokens_raw) if audio_tokens_raw is not None else None
            )
        except (TypeError, ValueError):
            audio_tokens = None

        return ASRResult(
            text=text,
            language=language,
            segments=[],  # MiMo does not return segments / timestamps
            duration=duration,
            model=body.get("model", "mimo-v2.5-asr"),
            audio_tokens=audio_tokens,
            raw_response=body,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


def create_mimo_backend(settings: BiliSettings) -> MimoASRBackend:
    """Factory: build a :class:`MimoASRBackend` from env settings.

    Resolves the base URL from ``bili_processing_asr_profile`` (or from
    ``bili_processing_asr_base_url`` when profile=='custom'). Raises
    :class:`ASRConfigError` on profile / base_url misconfiguration; an empty
    API key is permitted at construction so the wizard can still introspect
    the backend, but :meth:`MimoASRBackend.transcribe` will refuse to call
    the API without one.
    """
    base_url = resolve_base_url(
        settings.bili_processing_asr_profile,
        settings.bili_processing_asr_base_url,
    )
    return MimoASRBackend(
        api_key=settings.bili_processing_asr_api_key,
        base_url=base_url,
        model=settings.bili_processing_asr_model,
        timeout=settings.bili_processing_asr_timeout,
        max_completion_tokens=settings.bili_processing_asr_max_completion_tokens,
        auth_style=settings.bili_processing_asr_auth_style,
    )
