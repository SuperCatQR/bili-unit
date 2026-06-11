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


# ---------- profile resolution / auth_style / config errors -----------------


def test_resolve_base_url_known_profiles():
    from bili_unit.processing.audio._mimo_backend import (
        PROFILE_BASE_URLS,
        resolve_base_url,
    )

    assert resolve_base_url("token_plan_cn") == PROFILE_BASE_URLS["token_plan_cn"]
    assert resolve_base_url("token_plan_sgp") == PROFILE_BASE_URLS["token_plan_sgp"]
    assert resolve_base_url("token_plan_ams") == PROFILE_BASE_URLS["token_plan_ams"]
    assert resolve_base_url("pay_as_you_go") == PROFILE_BASE_URLS["pay_as_you_go"]
    # Trailing slash on custom URL is stripped to match preset normalisation.
    assert (
        resolve_base_url("custom", "https://relay.example.com/v1/")
        == "https://relay.example.com/v1"
    )


def test_resolve_base_url_unknown_profile_raises():
    from bili_unit.processing import ASRConfigError
    from bili_unit.processing.audio._mimo_backend import resolve_base_url

    with pytest.raises(ASRConfigError, match="Unknown ASR profile"):
        resolve_base_url("token_plan_jp")


def test_resolve_base_url_custom_without_url_raises():
    from bili_unit.processing import ASRConfigError
    from bili_unit.processing.audio._mimo_backend import resolve_base_url

    with pytest.raises(ASRConfigError, match="requires BILI_PROCESSING_ASR_BASE_URL"):
        resolve_base_url("custom", "")


def test_mimo_backend_auth_style_bearer_header():
    """auth_style='bearer' produces Authorization header instead of api-key."""
    backend = MimoASRBackend(api_key="tp-test-key", auth_style="bearer")
    h = backend._build_headers()
    assert h["Authorization"] == "Bearer tp-test-key"
    assert "api-key" not in h


def test_mimo_backend_auth_style_api_key_header_default():
    backend = MimoASRBackend(api_key="tp-test-key")  # default auth_style
    h = backend._build_headers()
    assert h["api-key"] == "tp-test-key"
    assert "Authorization" not in h


def test_mimo_backend_unknown_auth_style_raises():
    from bili_unit.processing import ASRConfigError

    with pytest.raises(ASRConfigError, match="Unknown auth_style"):
        MimoASRBackend(api_key="tp-test-key", auth_style="oauth")


@pytest.mark.asyncio
async def test_mimo_backend_transcribe_empty_key_raises_config_error():
    """Empty key fails fast with ASRConfigError, not a network round-trip."""
    from bili_unit.processing import ASRConfigError

    backend = MimoASRBackend(api_key="")
    with pytest.raises(ASRConfigError, match="API key is empty"):
        await backend.transcribe(b"\x00" * 100)


def test_create_mimo_backend_resolves_profile_from_settings():
    """create_mimo_backend reads profile/base_url from ProcessingEnv."""
    from bili_unit.processing.audio._mimo_backend import (
        PROFILE_BASE_URLS,
        create_mimo_backend,
    )
    from bili_unit.processing.env import ProcessingEnv

    s = ProcessingEnv(
        bili_processing_asr_backend="mimo",
        bili_processing_asr_profile="token_plan_sgp",
        bili_processing_asr_api_key="tp-test",
    )
    backend = create_mimo_backend(s)
    assert backend.base_url == PROFILE_BASE_URLS["token_plan_sgp"]
    assert backend.auth_style == "api_key"


def test_create_mimo_backend_custom_profile_uses_base_url_setting():
    from bili_unit.processing.audio._mimo_backend import create_mimo_backend
    from bili_unit.processing.env import ProcessingEnv

    s = ProcessingEnv(
        bili_processing_asr_backend="mimo",
        bili_processing_asr_profile="custom",
        bili_processing_asr_base_url="https://relay.example.com/v1",
        bili_processing_asr_auth_style="bearer",
        bili_processing_asr_api_key="my-relay-key",
    )
    backend = create_mimo_backend(s)
    assert backend.base_url == "https://relay.example.com/v1"
    assert backend.auth_style == "bearer"


def test_create_mimo_backend_custom_without_base_url_raises():
    from bili_unit.processing import ASRConfigError
    from bili_unit.processing.audio._mimo_backend import create_mimo_backend
    from bili_unit.processing.env import ProcessingEnv

    s = ProcessingEnv(
        bili_processing_asr_backend="mimo",
        bili_processing_asr_profile="custom",
        bili_processing_asr_base_url="",
        bili_processing_asr_api_key="tp-test",
    )
    with pytest.raises(ASRConfigError):
        create_mimo_backend(s)


def test_processing_runner_treats_asr_config_error_as_non_retryable():
    """ASRConfigError is an AudioError subclass but explicitly non-retryable."""
    from bili_unit.processing import ASRConfigError, ConvertError
    from bili_unit.processing.runner import ProcessingRunner

    assert ProcessingRunner._is_retryable(ASRConfigError("no key")) is False
    # Other AudioError subclasses remain retryable (regression guard).
    assert ProcessingRunner._is_retryable(ConvertError("bad mp3")) is True


# ---------- init_wizard -----------------------------------------------------


def _make_reader(answers):
    """Build a reader callable that pops queued answers in order."""
    it = iter(answers)
    return lambda _prompt: next(it)


def test_init_wizard_collect_token_plan_cn():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader([
        "1",            # profile choice → token_plan_cn
        "tp-mykey",     # api key
    ])
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_BACKEND"] == "mimo"
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "token_plan_cn"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "tp-mykey"
    assert fields["BILI_PROCESSING_ASR_AUTH_STYLE"] == "api_key"
    assert fields["BILI_PROCESSING_ASR_BASE_URL"] == ""


def test_init_wizard_collect_pay_as_you_go():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader([
        "4",            # pay_as_you_go
        "sk-test123",   # api key
    ])
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "pay_as_you_go"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "sk-test123"


def test_init_wizard_collect_custom_profile():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader([
        "5",                                     # custom profile
        "https://relay.example.com/v1/",         # base url (trailing slash to test strip)
        "2",                                     # auth_style → bearer
        "relay-key-xyz",                         # api key
    ])
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "custom"
    assert fields["BILI_PROCESSING_ASR_BASE_URL"] == "https://relay.example.com/v1"
    assert fields["BILI_PROCESSING_ASR_AUTH_STYLE"] == "bearer"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "relay-key-xyz"


def test_init_wizard_reprompts_on_invalid_then_succeeds():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader([
        "9",            # invalid profile (>5)
        "abc",          # invalid (non-digit)
        "1",            # valid → token_plan_cn
        "",             # empty key → reprompt
        "tp-key",       # valid key
    ])
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "token_plan_cn"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "tp-key"


def test_init_wizard_write_env_appends_and_overwrites(tmp_path):
    from bili_unit.processing.audio._init_wizard import write_env

    env_path = tmp_path / ".env"
    # Pre-existing content: a fetching cred line + a stale ASR config the
    # wizard should overwrite.
    env_path.write_text(
        "\n".join([
            "BILI_SESSDATA=keep-me",
            "BILI_PROCESSING_ASR_BACKEND=mock",
            "BILI_PROCESSING_ASR_API_KEY=stale-key",
            "# A comment to preserve",
            "BILI_PROCESSING_ASR_LANGUAGE=zh",  # unmanaged ASR_* key, must survive
        ]) + "\n",
        encoding="utf-8",
    )

    fields = {
        "BILI_PROCESSING_ASR_BACKEND": "mimo",
        "BILI_PROCESSING_ASR_PROFILE": "token_plan_cn",
        "BILI_PROCESSING_ASR_API_KEY": "tp-fresh",
        "BILI_PROCESSING_ASR_BASE_URL": "",
        "BILI_PROCESSING_ASR_AUTH_STYLE": "api_key",
    }
    write_env(fields, env_path=env_path)

    text = env_path.read_text(encoding="utf-8")
    # Fetching cred preserved.
    assert "BILI_SESSDATA=keep-me" in text
    # Comment preserved.
    assert "# A comment to preserve" in text
    # Unmanaged ASR_LANGUAGE preserved.
    assert "BILI_PROCESSING_ASR_LANGUAGE=zh" in text
    # New values present, stale gone.
    assert "BILI_PROCESSING_ASR_BACKEND=mimo" in text
    assert "BILI_PROCESSING_ASR_API_KEY=tp-fresh" in text
    assert "stale-key" not in text


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
    assert result[0].path.name == "full.mp3"
    assert result[0].start_s == 0.0
    assert result[0].end_s == 60.0
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


# ---------- convert_single VAD routing --------------------------------------


@pytest.mark.asyncio
async def test_convert_single_uses_vad_when_token_budget_triggers(tmp_path):
    """Long clip + use_vad=True → routes through detect_speech_segments + convert_at_points."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_points: dict = {}
    detect_calls: list = []
    fixed_seg_calls: list = []

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"x" * 1024)
        return Path(output_path)

    async def fake_detect(input_path, *, ffmpeg_setting, threshold,  # noqa: ARG001
                          min_silence_sec, min_speech_sec):
        detect_calls.append({"threshold": threshold})
        # One big silence gap from 700-720 — pick_split_points should cut at 710.
        return [(0.0, 700.0), (720.0, 1033.0)]

    async def fake_at_points(input_path, output_dir, points, ffmpeg_setting):  # noqa: ARG001
        captured_points["points"] = points
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(len(points)):
            f = out / f"seg_{i:03d}.mp3"
            f.write_bytes(b"y")
            files.append(f)
        return files

    async def fake_fixed_segment(*args, **kwargs):  # noqa: ARG001
        fixed_seg_calls.append((args, kwargs))
        return []

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "detect_speech_segments", side_effect=fake_detect),
        patch.object(conv, "convert_at_points", side_effect=fake_at_points),
        patch.object(conv, "convert_and_segment", side_effect=fake_fixed_segment),
    ):
        result = await conv.convert_single(
            in_path,
            out_dir,
            duration_seconds=1033.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
            use_vad=True,
            vad_threshold=0.3,
        )

    assert len(result) == 2
    # VAD path used, not the fixed-period fallback.
    assert detect_calls == [{"threshold": 0.3}]
    assert fixed_seg_calls == []
    # The plan must reflect the silence-aware cut at the gap midpoint.
    points = captured_points["points"]
    assert points[0] == (0.0, 710.0)
    assert points[1] == (710.0, 1033.0)


@pytest.mark.asyncio
async def test_convert_single_vad_disabled_uses_fixed_segmentation(tmp_path):
    """use_vad=False → token-budget path uses convert_and_segment (old behaviour)."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_seg: dict = {}
    detect_calls: list = []

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"x" * 1024)
        return Path(output_path)

    async def fake_detect(*args, **kwargs):  # noqa: ARG001
        detect_calls.append(1)
        return []

    async def fake_segment(input_path, output_dir, segment_seconds, ffmpeg_setting):  # noqa: ARG001
        captured_seg["segment_seconds"] = segment_seconds
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        f = out / "seg_000.mp3"
        f.write_bytes(b"y")
        return [f]

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "detect_speech_segments", side_effect=fake_detect),
        patch.object(conv, "convert_and_segment", side_effect=fake_segment),
    ):
        await conv.convert_single(
            in_path,
            out_dir,
            duration_seconds=1033.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
            use_vad=False,
        )

    # VAD must not be called.
    assert detect_calls == []
    assert captured_seg["segment_seconds"] == 830


@pytest.mark.asyncio
async def test_convert_single_vad_failure_falls_back_to_fixed(tmp_path):
    """VAD detection raising → graceful fallback to convert_and_segment."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_seg: dict = {}

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"x" * 1024)
        return Path(output_path)

    async def fake_detect(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("onnxruntime missing")

    async def fake_segment(input_path, output_dir, segment_seconds, ffmpeg_setting):  # noqa: ARG001
        captured_seg["segment_seconds"] = segment_seconds
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        f = out / "seg_000.mp3"
        f.write_bytes(b"y")
        return [f]

    with (
        patch.object(conv, "convert_m4s_to_mp3", side_effect=fake_convert),
        patch.object(conv, "detect_speech_segments", side_effect=fake_detect),
        patch.object(conv, "convert_and_segment", side_effect=fake_segment),
    ):
        result = await conv.convert_single(
            in_path,
            out_dir,
            duration_seconds=1033.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
            use_vad=True,
        )

    # Fell back to fixed-period segmentation despite use_vad=True.
    assert len(result) == 1
    assert captured_seg["segment_seconds"] == 830
