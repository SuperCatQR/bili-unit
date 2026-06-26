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


def test_mimo_backend_rejects_high_risk_refusal_text():
    """MiMo safety refusals are API failures, not successful transcripts."""
    from bili_unit.processing import ASRAPIError

    body = {
        "choices": [
            {
                "message": {
                    "content": ("The request was rejected because it was considered high risk"),
                },
            },
        ],
        "usage": {
            "seconds": 1,
            "prompt_tokens_details": {"audio_tokens": 1},
        },
        "model": "mimo-v2.5-asr",
    }

    with pytest.raises(ASRAPIError, match="high risk"):
        MimoASRBackend._parse_response(body, language="auto")


def test_mimo_backend_rejects_empty_transcript_text():
    from bili_unit.processing import EmptyTranscriptError

    body = {
        "choices": [{"message": {"content": "   "}}],
        "usage": {
            "seconds": 1,
            "prompt_tokens_details": {"audio_tokens": 1},
        },
        "model": "mimo-v2.5-asr",
    }

    with pytest.raises(EmptyTranscriptError, match="empty transcription text"):
        MimoASRBackend._parse_response(body, language="auto")


def test_mimo_backend_rejects_truncated_transcript():
    from bili_unit.processing import ASRAPIError

    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "partial transcript"},
            },
        ],
        "usage": {
            "seconds": 180,
            "prompt_tokens_details": {"audio_tokens": 1000},
        },
        "model": "mimo-v2.5-asr",
    }

    with pytest.raises(ASRAPIError, match="truncated"):
        MimoASRBackend._parse_response(body, language="auto")


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
    assert resolve_base_url("custom", "https://relay.example.com/v1/") == "https://relay.example.com/v1"


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
    """create_mimo_backend reads profile/base_url from BiliSettings."""
    from bili_unit._env import BiliSettings
    from bili_unit.processing.audio._mimo_backend import (
        PROFILE_BASE_URLS,
        create_mimo_backend,
    )

    s = BiliSettings(
        bili_processing_asr_backend="mimo",
        bili_processing_asr_profile="token_plan_sgp",
        bili_processing_asr_api_key="tp-test",
    )
    backend = create_mimo_backend(s)
    assert backend.base_url == PROFILE_BASE_URLS["token_plan_sgp"]
    assert backend.auth_style == "api_key"


def test_create_mimo_backend_custom_profile_uses_base_url_setting():
    from bili_unit._env import BiliSettings
    from bili_unit.processing.audio._mimo_backend import create_mimo_backend

    s = BiliSettings(
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
    from bili_unit._env import BiliSettings
    from bili_unit.processing import ASRConfigError
    from bili_unit.processing.audio._mimo_backend import create_mimo_backend

    s = BiliSettings(
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

    reader = _make_reader(
        [
            "1",  # profile choice → token_plan_cn
            "tp-mykey",  # api key
        ]
    )
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_BACKEND"] == "mimo"
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "token_plan_cn"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "tp-mykey"
    assert fields["BILI_PROCESSING_ASR_AUTH_STYLE"] == "api_key"
    assert fields["BILI_PROCESSING_ASR_BASE_URL"] == ""


def test_init_wizard_collect_pay_as_you_go():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader(
        [
            "4",  # pay_as_you_go
            "sk-test123",  # api key
        ]
    )
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "pay_as_you_go"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "sk-test123"


def test_init_wizard_collect_custom_profile():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader(
        [
            "5",  # custom profile
            "https://relay.example.com/v1/",  # base url (trailing slash to test strip)
            "2",  # auth_style → bearer
            "relay-key-xyz",  # api key
        ]
    )
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "custom"
    assert fields["BILI_PROCESSING_ASR_BASE_URL"] == "https://relay.example.com/v1"
    assert fields["BILI_PROCESSING_ASR_AUTH_STYLE"] == "bearer"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "relay-key-xyz"


def test_init_wizard_reprompts_on_invalid_then_succeeds():
    from bili_unit.processing.audio._init_wizard import collect_config

    reader = _make_reader(
        [
            "9",  # invalid profile (>5)
            "abc",  # invalid (non-digit)
            "1",  # valid → token_plan_cn
            "",  # empty key → reprompt
            "tp-key",  # valid key
        ]
    )
    fields = collect_config(reader=reader)
    assert fields["BILI_PROCESSING_ASR_PROFILE"] == "token_plan_cn"
    assert fields["BILI_PROCESSING_ASR_API_KEY"] == "tp-key"


def test_init_wizard_write_env_appends_and_overwrites(tmp_path):
    from bili_unit.processing.audio._init_wizard import write_env

    env_path = tmp_path / ".env"
    # Pre-existing content: a fetching cred line + a stale ASR config the
    # wizard should overwrite.
    env_path.write_text(
        "\n".join(
            [
                "BILI_SESSDATA=keep-me",
                "BILI_PROCESSING_ASR_BACKEND=mock",
                "BILI_PROCESSING_ASR_API_KEY=stale-key",
                "# A comment to preserve",
                "BILI_PROCESSING_ASR_LANGUAGE=zh",  # unmanaged ASR_* key, must survive
            ]
        )
        + "\n",
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


def test_init_mimo_parser_accepts_test_probe_flag():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["init-mimo", "--test"])
    assert args.command == "init-mimo"
    assert args.test is True


@pytest.mark.asyncio
async def test_init_mimo_probe_calls_backend_with_tiny_wav():
    from bili_unit._env import BiliSettings
    from bili_unit.processing.audio._init_wizard import probe_mimo_model

    captured: dict = {}

    class FakeBackend:
        async def transcribe(self, audio_data, mime_type="audio/mp3", language="auto"):
            captured["audio_data"] = audio_data
            captured["mime_type"] = mime_type
            captured["language"] = language
            return ASRResult(
                text="",
                language=language,
                duration=1.0,
                model="mimo-v2.5-asr",
                audio_tokens=2,
                raw_response={"ok": True},
            )

        async def close(self):
            captured["closed"] = True

    settings = BiliSettings(
        bili_processing_asr_api_key="tp-test",
        bili_processing_asr_language="zh",
    )

    result = await probe_mimo_model(
        settings=settings,
        backend_factory=lambda _settings: FakeBackend(),
    )

    assert result.model == "mimo-v2.5-asr"
    assert captured["audio_data"].startswith(b"RIFF")
    assert captured["mime_type"] == "audio/wav"
    assert captured["language"] == "zh"
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_init_mimo_handler_runs_probe_when_requested(monkeypatch, capsys):
    from bili_unit import __main__ as cli

    called: dict = {}

    def fake_reload_settings():
        called["reload"] = True

    async def fake_probe_mimo_model():
        called["probe"] = True
        return ASRResult(
            text="probe ok",
            language="auto",
            duration=1.0,
            model="mimo-v2.5-asr",
            audio_tokens=2,
            raw_response={"ok": True},
        )

    monkeypatch.setattr(
        "bili_unit.processing.audio._init_wizard.run_wizard",
        lambda: Path(".env"),
    )
    monkeypatch.setattr(
        "bili_unit.processing.audio._init_wizard.probe_mimo_model",
        fake_probe_mimo_model,
    )
    monkeypatch.setattr("bili_unit._env.reload_settings", fake_reload_settings)

    await cli._handle_init_mimo(type("Args", (), {"test": True})())

    out = capsys.readouterr().out
    assert called == {"reload": True, "probe": True}
    assert "MiMo probe OK" in out
    assert "mimo-v2.5-asr" in out


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
    mock_resp.json = AsyncMock(
        return_value={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"seconds": 1},
            "model": "mimo-v2.5-asr",
        }
    )
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
    mock_resp.json = AsyncMock(
        return_value={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"seconds": 1},
            "model": "mimo-v2.5-asr",
        }
    )
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
async def test_convert_single_segment_cap_overrides_token_budget(tmp_path):
    """A stricter max_segment_seconds cap protects ASR output completeness."""
    from bili_unit.processing.audio import _converter as conv

    in_path = tmp_path / "audio.m4s"
    in_path.write_bytes(b"\x00")
    out_dir = tmp_path / "out"

    captured_seg: dict = {}

    async def fake_convert(input_path, output_path, ffmpeg_setting="auto"):  # noqa: ARG001
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"x" * 1024)
        return Path(output_path)

    async def fake_segment(input_path, output_dir, segment_seconds, ffmpeg_setting):  # noqa: ARG001
        captured_seg["segment_seconds"] = segment_seconds
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = [out / f"seg_{i:03d}.mp3" for i in range(6)]
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
            duration_seconds=1033.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
            max_segment_seconds=120,
            use_vad=False,
        )

    assert len(result) == 6
    assert captured_seg["segment_seconds"] == 120


@pytest.mark.asyncio
async def test_convert_single_segment_cap_still_uses_vad(tmp_path):
    """max_segment_seconds is a ceiling; VAD still chooses dynamic cut points."""
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

    async def fake_detect(
        input_path,
        *,
        ffmpeg_setting,
        threshold,  # noqa: ARG001
        min_silence_sec,
        min_speech_sec,
    ):
        detect_calls.append({"threshold": threshold})
        return [
            (0.0, 108.0),
            (112.0, 218.0),
            (222.0, 328.0),
            (332.0, 438.0),
            (442.0, 500.0),
        ]

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
            duration_seconds=500.0,
            max_input_tokens=5400,
            tokens_per_second=6.5,
            max_segment_seconds=120,
            use_vad=True,
            vad_threshold=0.3,
        )

    assert len(result) == 5
    assert detect_calls == [{"threshold": 0.3}]
    assert fixed_seg_calls == []
    assert captured_points["points"] == [
        (0.0, 110.0),
        (110.0, 220.0),
        (220.0, 330.0),
        (330.0, 440.0),
        (440.0, 500.0),
    ]


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

    async def fake_detect(
        input_path,
        *,
        ffmpeg_setting,
        threshold,  # noqa: ARG001
        min_silence_sec,
        min_speech_sec,
    ):
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


# ---------- AudioDownloader: size cap + timeout ----------------------------


@pytest.mark.asyncio
async def test_audio_downloader_size_cap():
    """iter_chunked yielding more bytes than max_size_bytes raises DownloadError."""
    import contextlib
    import os
    import tempfile
    from unittest.mock import AsyncMock, MagicMock, patch

    from bili_unit.processing import DownloadError
    from bili_unit.processing.audio._downloader import AudioDownloader

    cap = 100
    downloader = AudioDownloader(download_timeout_s=30, max_size_bytes=cap)

    # Build a fake chunk iterator that yields cap+1 bytes in one shot.
    async def _fake_iter_chunked(size):  # noqa: ARG001
        yield b"x" * (cap + 1)

    fake_content = MagicMock()
    fake_content.iter_chunked = _fake_iter_chunked

    fake_resp = AsyncMock()
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)
    fake_resp.status = 200
    fake_resp.content = fake_content

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.get = MagicMock(return_value=fake_resp)

    with patch("bili_unit.processing.audio._downloader.aiohttp.ClientSession", return_value=fake_session):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            dest = tf.name
        try:
            with pytest.raises(DownloadError, match="exceeded"):
                await downloader.download_to_file("http://fake/url", dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(dest)


@pytest.mark.asyncio
async def test_audio_downloader_timeout_set():
    """AudioDownloader passes download_timeout_s as ClientTimeout.total."""
    import contextlib
    import os
    import tempfile
    from unittest.mock import AsyncMock, MagicMock, patch

    from bili_unit.processing.audio._downloader import AudioDownloader

    downloader = AudioDownloader(download_timeout_s=42)

    captured_timeout: list = []

    class FakeSession:
        def __init__(self, *, timeout=None, **kw):
            captured_timeout.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, url, headers=None):
            resp = AsyncMock()
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            resp.status = 200

            async def _empty(size):  # noqa: ARG001
                return
                yield  # make it an async generator

            resp.content = MagicMock()
            resp.content.iter_chunked = _empty
            return resp

    with patch("bili_unit.processing.audio._downloader.aiohttp.ClientSession", FakeSession):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            dest = tf.name
        with contextlib.suppress(Exception):
            await downloader.download_to_file("http://fake/url", dest)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(dest)

    assert len(captured_timeout) == 1
    assert captured_timeout[0].total == 42


# ---------- cleanup_orphan_temp_dirs ----------------------------------------


def test_cleanup_orphan_temp_dirs_removes_lone_full_mp3(tmp_path):
    """A full.mp3 with no sibling 'segments' dir and old mtime is removed."""
    import os
    import time

    from bili_unit.processing.audio._converter import cleanup_orphan_temp_dirs

    temp_root = tmp_path / "asr_temp"
    orphan = temp_root / "uid1" / "audio" / "BV1" / "mp3_0" / "full.mp3"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    # Backdate mtime past the threshold.
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    n = cleanup_orphan_temp_dirs(temp_root, max_age_seconds=10.0)
    assert n == 1
    assert not orphan.exists()


def test_cleanup_orphan_temp_dirs_keeps_recent_or_in_progress(tmp_path):
    """Recent full.mp3 OR a full.mp3 with sibling 'segments/' is kept."""
    import os
    import time

    from bili_unit.processing.audio._converter import cleanup_orphan_temp_dirs

    temp_root = tmp_path / "asr_temp"

    fresh = temp_root / "uid1" / "audio" / "BV1" / "mp3_0" / "full.mp3"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b"x")  # current mtime — under threshold

    in_progress = temp_root / "uid1" / "audio" / "BV2" / "mp3_0" / "full.mp3"
    in_progress.parent.mkdir(parents=True)
    in_progress.write_bytes(b"x")
    (in_progress.parent / "segments").mkdir()
    old = time.time() - 3600
    os.utime(in_progress, (old, old))

    n = cleanup_orphan_temp_dirs(temp_root, max_age_seconds=60.0)
    assert n == 0
    assert fresh.exists()
    assert in_progress.exists()


def test_cleanup_orphan_temp_dirs_missing_root_returns_zero(tmp_path):
    """Non-existent temp root returns 0 without raising."""
    from bili_unit.processing.audio._converter import cleanup_orphan_temp_dirs

    assert cleanup_orphan_temp_dirs(tmp_path / "does_not_exist") == 0


# ---------- A2: LengthTruncatedError + per-call max_tokens override ----------


def test_mimo_backend_raises_length_truncated_error_on_finish_reason_length():
    """``finish_reason='length'`` raises the specific ``LengthTruncatedError``.

    Subclass of ``ASRAPIError`` so existing ``except ASRAPIError`` paths keep
    catching it, but the runner can pivot on the precise type to grow
    ``max_completion_tokens`` and split.
    """
    from bili_unit.processing import ASRAPIError, LengthTruncatedError

    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "partial transcript"},
            },
        ],
        "usage": {
            "seconds": 180,
            "prompt_tokens_details": {"audio_tokens": 1000},
        },
        "model": "mimo-v2.5-asr",
    }

    with pytest.raises(LengthTruncatedError, match="truncated"):
        MimoASRBackend._parse_response(body, language="auto")
    # Subclass relationship — generic ASRAPIError handlers still catch it.
    assert issubclass(LengthTruncatedError, ASRAPIError)


@pytest.mark.asyncio
async def test_mimo_backend_per_call_max_tokens_override():
    """Passing ``max_completion_tokens`` to ``transcribe`` overrides the instance default."""
    backend = MimoASRBackend(
        api_key="tp-test-key",
        max_completion_tokens=1024,  # instance default
    )

    captured: dict = {}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"seconds": 1},
            "model": "mimo-v2.5-asr",
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    def fake_post(url, json=None, headers=None):  # noqa: ARG001
        captured["payload"] = json
        return mock_resp

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=fake_post)
    mock_session.closed = False

    with patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_session)):
        await backend.transcribe(
            b"\x00" * 16,
            mime_type="audio/mp3",
            max_completion_tokens=4096,  # per-call override
        )

    assert captured["payload"]["max_tokens"] == 4096


# ---------- A2: _doubling_attempts pure helper -------------------------------


def test_doubling_attempts_helper():
    from bili_unit.processing.runner._audio_work import _doubling_attempts

    assert _doubling_attempts(1024, 8192) == [1024, 2048, 4096, 8192]
    assert _doubling_attempts(2048, 8192) == [2048, 4096, 8192]
    # When start equals cap, single attempt at the cap.
    assert _doubling_attempts(8192, 8192) == [8192]
    # When start exceeds cap, cap is honoured (single attempt at the cap).
    assert _doubling_attempts(16384, 8192) == [8192]
    # Non-positive inputs return empty (no attempts) so the caller raises.
    assert _doubling_attempts(0, 8192) == []
    assert _doubling_attempts(1024, 0) == []


# ---------- C3: audio_failure_category ---------------------------------------


def test_audio_failure_category_covers_each_branch():
    from bili_unit.processing import (
        ASRAPIError,
        ASRConnectionError,
        EmptyTranscriptError,
        LengthTruncatedError,
    )
    from bili_unit.processing.runner._audio_work import audio_failure_category

    assert audio_failure_category(LengthTruncatedError("x")) == "max_tokens"
    assert audio_failure_category(ASRConnectionError("net down")) == "network"
    # rate-limit detection runs against the message text on ASRAPIError.
    assert audio_failure_category(ASRAPIError("MiMo ASR returned 429: too many requests")) == "rate_limit"
    assert audio_failure_category(ASRAPIError("rate limit exceeded")) == "rate_limit"
    # high-risk detection ditto.
    assert audio_failure_category(ASRAPIError("considered high risk")) == "high_risk"
    # empty transcript via the dedicated exception.
    assert audio_failure_category(EmptyTranscriptError("nothing said")) == "empty"
    # Generic ASRAPIError that doesn't match a known pattern → parse_error.
    assert audio_failure_category(ASRAPIError("malformed JSON")) == "parse_error"
    # Anything else → unknown.
    assert audio_failure_category(RuntimeError("boom")) == "unknown"
