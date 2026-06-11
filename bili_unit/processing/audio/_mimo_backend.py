# audio/_mimo_backend — MiMo ASR backend via aiohttp.
#
# Per docs/design/processing.md §7.4:
#   POST {BASE_URL}/chat/completions with model=mimo-v2.5-asr.
#   Uses OpenAI-compatible ``input_audio`` content part with base64 data URI.
#
# Token Plan keys (tp-*) must use regional Token Plan hosts:
#   https://token-plan-cn.xiaomimimo.com/v1  (default)
# Pay-as-you-go keys (sk-*) use https://api.xiaomimimo.com/v1.

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import aiohttp

from .. import ASRAPIError, ASRConnectionError
from ._asr_backend import ASRResult

if TYPE_CHECKING:
    from ..env import ProcessingEnv

logger = logging.getLogger("bili.processing.audio.mimo")


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
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_completion_tokens = max_completion_tokens
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._model

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

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
        headers = {
            "api-key": self._api_key,
            "Content-Type": "application/json",
        }

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
        """
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ASRAPIError(
                f"unexpected MiMo response shape: {exc}"
            ) from exc

        usage = body.get("usage", {})
        duration_raw = usage.get("seconds")
        duration: float | None = (
            float(duration_raw) if duration_raw is not None else None
        )

        return ASRResult(
            text=text,
            language=language,
            segments=[],  # MiMo does not return segments / timestamps
            duration=duration,
            model=body.get("model", "mimo-v2.5-asr"),
            raw_response=body,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


def create_mimo_backend(settings: ProcessingEnv) -> MimoASRBackend:
    """Factory: build a :class:`MimoASRBackend` from env settings."""
    return MimoASRBackend(
        api_key=settings.bili_processing_asr_api_key,
        base_url=settings.bili_processing_asr_base_url,
        model=settings.bili_processing_asr_model,
        timeout=settings.bili_processing_asr_timeout,
        max_completion_tokens=settings.bili_processing_asr_max_completion_tokens,
    )
