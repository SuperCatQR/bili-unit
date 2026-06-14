# audio/_asr_backend — ASRBackend protocol + MockASRBackend + factory.
#
# ASR backend abstraction with multiple impls (mimo / whisper / mock).

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ASRResult:
    text: str
    language: str | None = None
    segments: list[dict] = field(default_factory=list)
    duration: float | None = None
    model: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


class ASRBackend(Protocol):
    """Protocol every ASR backend implementation must satisfy."""

    async def transcribe(
        self,
        audio_data: bytes,
        mime_type: str = "audio/mp3",
        language: str = "auto",
    ) -> ASRResult: ...

    async def close(self) -> None: ...


class MockASRBackend:
    """Deterministic ASR backend used in MVP and tests.

    Returns a fixed transcription based on the audio_data length so tests
    can assert on output without external services.
    """

    model = "mock-asr-v0"

    def __init__(self, fixed_text: str = "(mock transcription)") -> None:
        self._text = fixed_text

    async def transcribe(
        self,
        audio_data: bytes,
        mime_type: str = "audio/mp3",
        language: str = "auto",
    ) -> ASRResult:
        # Length-derived "duration" so tests can distinguish payloads.
        duration = float(len(audio_data)) / 16000.0  # arbitrary scale
        return ASRResult(
            text=self._text,
            language=language if language != "auto" else "zh",
            segments=[],
            duration=duration,
            model=self.model,
            raw_response={"mock": True, "bytes": len(audio_data)},
        )

    async def close(self) -> None:
        return None


def create_asr_backend(backend_name: str, **kw: Any) -> ASRBackend:
    """Factory for backend selection per BILI_PROCESSING_ASR_BACKEND.

    Supports ``mock`` (always) and ``mimo`` (requires api_key in *kw* or
    settings).  ``whisper`` is scheduled for a future batch.
    """
    name = (backend_name or "").lower().strip()
    if name in ("", "mock"):
        return MockASRBackend()
    if name == "mimo":
        from ._mimo_backend import create_mimo_backend

        settings = kw.get("settings")
        if settings is None:
            from ..._env import get_settings

            settings = get_settings()
        return create_mimo_backend(settings)
    if name == "whisper":
        raise NotImplementedError(
            "ASR backend 'whisper' is scheduled for a future batch."
        )
    raise ValueError(f"unknown ASR backend: {backend_name!r}")
