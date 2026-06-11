# tests for bili_unit/processing/audio backend.

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bili_unit.processing.audio import (
    ASRResult,
    FFmpegUnavailable,
    MimoASRBackend,
    MockASRBackend,
    compute_segment_seconds,
    create_asr_backend,
    is_available,
    resolve_ffmpeg,
)
from bili_unit.processing.audio._ffmpeg import resolve_ffmpeg as _resolve

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------- MockASRBackend --------------------------------------------------

@pytest.mark.asyncio
async def test_mock_asr_backend_returns_result():
    backend = MockASRBackend(fixed_text="hello world")
    result = await backend.transcribe(b"\x00" * 1024, mime_type="audio/mp3", language="zh")
    assert isinstance(result, ASRResult)
    assert result.text == "hello world"
    assert result.language == "zh"
    assert result.model == "mock-asr-v0"
    assert result.duration is not None
    assert result.raw_response["mock"] is True
    await backend.close()


@pytest.mark.asyncio
async def test_mock_asr_backend_auto_language_defaults_to_zh():
    backend = MockASRBackend()
    result = await backend.transcribe(b"abc", language="auto")
    assert result.language == "zh"


# ---------- create_asr_backend factory --------------------------------------

def test_create_asr_backend_mock():
    assert isinstance(create_asr_backend("mock"), MockASRBackend)
    assert isinstance(create_asr_backend(""), MockASRBackend)


def test_create_asr_backend_mimo():
    """MimoASRBackend is now fully implemented (no longer NotImplementedError)."""
    backend = create_asr_backend("mimo")
    assert isinstance(backend, MimoASRBackend)


def test_create_asr_backend_whisper_unimplemented():
    with pytest.raises(NotImplementedError):
        create_asr_backend("whisper")


def test_create_asr_backend_unknown():
    with pytest.raises(ValueError):
        create_asr_backend("unknown-engine")


# ---------- MimoASRBackend --------------------------------------------------

@pytest.mark.asyncio
async def test_mimo_backend_transcribe_with_fixture():
    """MimoASRBackend parses a real MiMo response fixture correctly."""
    fixture_path = _FIXTURES / "mimo_asr_response.json"
    fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    backend = MimoASRBackend(
        api_key="tp-test-key",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        model="mimo-v2.5-asr",
        timeout=30,
    )

    # Mock aiohttp session.post() to return the fixture.
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=fixture_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    with patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_session)):
        result = await backend.transcribe(
            b"\x00" * 1024,
            mime_type="audio/mp3",
            language="auto",
        )

    assert isinstance(result, ASRResult)
    expected_text = fixture_data["choices"][0]["message"]["content"]
    assert result.text == expected_text
    assert result.duration == 134.0
    assert result.language == "auto"
    assert result.model == "mimo-v2.5-asr"
    assert result.segments == []
    assert result.raw_response["usage"]["seconds"] == 134
    assert result.raw_response["usage"]["prompt_tokens_details"]["audio_tokens"] == 837


@pytest.mark.asyncio
async def test_mimo_backend_api_error_raises():
    """MimoASRBackend raises ASRAPIError on non-200 response."""
    from bili_unit.processing import ASRAPIError

    backend = MimoASRBackend(api_key="tp-test-key")

    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={"error": "Invalid API Key"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    with (
        patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_session)),
        pytest.raises(ASRAPIError, match="401"),
    ):
        await backend.transcribe(b"\x00" * 100)


@pytest.mark.asyncio
async def test_mimo_backend_close():
    """close() closes the internal session and resets it."""
    backend = MimoASRBackend(api_key="tp-test-key")

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    backend._session = mock_session

    await backend.close()
    mock_session.close.assert_awaited_once()
    assert backend._session is None


# ---------- ffmpeg discovery -------------------------------------------------

def test_resolve_ffmpeg_auto_prefers_system_or_falls_back(monkeypatch):
    """When auto, system path wins; if system missing, fall back to imageio."""
    _resolve.cache_clear()
    monkeypatch.setattr("shutil.which", lambda _name: None)
    path = resolve_ffmpeg("auto")
    assert path
    _resolve.cache_clear()


def test_resolve_ffmpeg_system_only_missing(monkeypatch):
    _resolve.cache_clear()
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(FFmpegUnavailable):
        resolve_ffmpeg("system")
    _resolve.cache_clear()


def test_resolve_ffmpeg_imageio_only():
    _resolve.cache_clear()
    path = resolve_ffmpeg("imageio")
    assert path
    _resolve.cache_clear()


def test_resolve_ffmpeg_explicit_path_returned():
    _resolve.cache_clear()
    custom = "C:\\fake\\path\\to\\ffmpeg.exe"
    assert resolve_ffmpeg(custom) == custom
    _resolve.cache_clear()


def test_is_available_auto():
    _resolve.cache_clear()
    assert is_available("auto") is True
    _resolve.cache_clear()


# ---------- compute_segment_seconds (token-budget) ---------------------------

def test_compute_segment_seconds_short_clip_returns_none():
    """Clip already under budget — caller should not segment."""
    # 60 s * 6.5 t/s = 390 tokens, under 5400 budget.
    assert compute_segment_seconds(60.0, 5400, 6.5) is None


def test_compute_segment_seconds_long_clip_splits():
    """Clip over budget — return per-segment seconds that fit."""
    # 1033 s (the failing case from uid 3546785614137774).
    seg = compute_segment_seconds(1033.0, 5400, 6.5)
    assert seg is not None
    # Each segment must keep estimated tokens ≤ budget.
    assert seg * 6.5 <= 5400
    # 5400 / 6.5 = 830 → expect 830 seconds.
    assert seg == 830


def test_compute_segment_seconds_floors_at_60_seconds():
    """Pathological budget config never yields <60 s segments."""
    # tokens_per_second very high relative to budget.
    seg = compute_segment_seconds(1000.0, 100, 100.0)
    assert seg == 60


def test_compute_segment_seconds_handles_invalid_inputs():
    """Non-positive inputs → no segmentation hint."""
    assert compute_segment_seconds(0.0, 5400, 6.5) is None
    assert compute_segment_seconds(100.0, 0, 6.5) is None
    assert compute_segment_seconds(100.0, 5400, 0.0) is None


# ---------- max_completion_tokens payload -----------------------------------

@pytest.mark.asyncio
async def test_mimo_backend_passes_max_completion_tokens():
    """When max_completion_tokens is set, payload must include max_tokens."""
    backend = MimoASRBackend(
        api_key="tp-test-key",
        max_completion_tokens=1024,
    )

    captured: dict = {}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"seconds": 1},
        "model": "mimo-v2.5-asr",
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    def fake_post(url, json=None, headers=None):  # noqa: ARG001
        captured["payload"] = json
        return mock_resp

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=fake_post)
    mock_session.closed = False

    with patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_session)):
        await backend.transcribe(b"\x00" * 16, mime_type="audio/mp3")

    assert captured["payload"]["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_mimo_backend_omits_max_tokens_when_none():
    """When max_completion_tokens is None, payload must NOT include max_tokens."""
    backend = MimoASRBackend(api_key="tp-test-key", max_completion_tokens=None)

    captured: dict = {}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"seconds": 1},
        "model": "mimo-v2.5-asr",
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    def fake_post(url, json=None, headers=None):  # noqa: ARG001
        captured["payload"] = json
        return mock_resp

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=fake_post)
    mock_session.closed = False

    with patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_session)):
        await backend.transcribe(b"\x00" * 16, mime_type="audio/mp3")

    assert "max_tokens" not in captured["payload"]


# ---------- convert_single decision tree -------------------------------------

@pytest.mark.asyncio
async def test_convert_single_token_budget_no_split(tmp_path):
    """Short clip + token info → returns single full mp3, no segmentation."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")  # ffmpeg call is mocked anyway
    out_dir = tmp_path / "out"

    full_mp3_calls: list = []
    seg_calls: list = []

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"x")
        full_mp3_calls.append(output_path)
        return Path(output_path)

    async def fake_segment(*args, **kwargs):  # noqa: ARG001
        seg_calls.append((args, kwargs))
        return []

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "convert_and_segment", side_effect=fake_segment),
    ):
        result = await conv.convert_single(
            in_path,
            out_dir,
            duration_seconds=60.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
        )

    assert len(result) == 1
    assert result[0].name == "full.mp3"
    assert seg_calls == []  # token budget was satisfied → no split


@pytest.mark.asyncio
async def test_convert_single_token_budget_splits(tmp_path):
    """Long clip + token info → token-budget split (not size-based)."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_seg: dict = {}

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # Tiny mp3 (well under 10 MB) — proves token path triggered, not size path.
        Path(output_path).write_bytes(b"x" * 1024)
        return Path(output_path)

    async def fake_segment(input_path, output_dir, segment_seconds, ffmpeg_setting):  # noqa: ARG001
        captured_seg["segment_seconds"] = segment_seconds
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = [out / "seg_000.mp3", out / "seg_001.mp3"]
        for f in files:
            f.write_bytes(b"y")
        return files

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "convert_and_segment", side_effect=fake_segment),
    ):
        result = await conv.convert_single(
            in_path,
            out_dir,
            duration_seconds=1033.0,  # the failing case
            max_input_tokens=5400,
            tokens_per_second=6.5,
        )

    assert len(result) == 2
    # Segment seconds must be the token-derived value, not 8*60=480.
    assert captured_seg["segment_seconds"] == 830


@pytest.mark.asyncio
async def test_convert_single_falls_back_to_size_when_no_duration(tmp_path):
    """No duration_seconds → use size-based fallback (preserves old behaviour)."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_seg: dict = {}

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # Big mp3 to trigger size-based split.
        Path(output_path).write_bytes(b"x" * (11 * 1024 * 1024))
        return Path(output_path)

    async def fake_segment(input_path, output_dir, segment_seconds, ffmpeg_setting):  # noqa: ARG001
        captured_seg["segment_seconds"] = segment_seconds
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        f = out / "seg_000.mp3"
        f.write_bytes(b"y")
        return [f]

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "convert_and_segment", side_effect=fake_segment),
    ):
        result = await conv.convert_single(
            in_path,
            out_dir,
            max_file_size_mb=10,
            segment_minutes=8,
        )

    assert len(result) == 1
    assert captured_seg["segment_seconds"] == 480  # 8 min
