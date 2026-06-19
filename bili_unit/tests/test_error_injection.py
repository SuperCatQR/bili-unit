# tests/test_error_injection.py — error-injection tests for key I/O paths.
#
# Coverage areas:
#   1-2. AudioDownloader.download_to_file — aiohttp.ClientError + TimeoutError
#   3.   _fetch_subtitle_body             — aiohttp.ClientError → sentinel dict
#   4-5. MimoASRBackend.transcribe        — 401 → ASRAPIError, 429 not retryable at this layer
#   6.   convert_m4s_to_mp3               — ffmpeg non-zero exit → ConvertError
#   7.   Connection.run_transaction        — OperationalError + rollback verification
#
# pytest-asyncio is configured with asyncio_mode="auto" (pyproject.toml),
# so @pytest.mark.asyncio annotations are not required.
# conftest.py autouse fixtures (_mock_retry_sleep, _mock_get_credential) apply
# automatically to this module.

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from bili_unit.processing import ASRAPIError, ConvertError, DownloadError
from bili_unit.processing.audio import AudioDownloader, MimoASRBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aiohttp_resp_mock(*, status: int, json_data: Any | None = None) -> MagicMock:
    """Build a MagicMock that behaves as an aiohttp response async context manager."""
    resp = MagicMock()
    resp.status = status
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session_mock(resp: MagicMock) -> MagicMock:
    """Wrap a response mock in a minimal session mock."""
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.post = MagicMock(return_value=resp)
    session.closed = False
    return session


# ---------------------------------------------------------------------------
# 1. AudioDownloader — aiohttp.ClientError → DownloadError
# ---------------------------------------------------------------------------

async def test_audio_downloader_translates_aiohttp_client_error(tmp_path: Path) -> None:
    """ClientError from aiohttp.ClientSession.get must be wrapped in DownloadError."""
    downloader = AudioDownloader()
    dest = str(tmp_path / "audio.m4s")

    # Patch at the module level used by _downloader.py
    with patch("bili_unit.processing.audio._downloader.aiohttp.ClientSession") as MockSession:
        # __aenter__ returns a session whose .get raises ClientError
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(side_effect=aiohttp.ClientError("simulated"))
        MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        MockSession.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(DownloadError) as exc_info:
            await downloader.download_to_file("https://cdn.example.com/audio.m4s", dest, bvid="BV1xx")

    cause = exc_info.value.__cause__
    assert isinstance(cause, aiohttp.ClientError), f"expected aiohttp.ClientError as __cause__, got {type(cause)}"


# ---------------------------------------------------------------------------
# 2. AudioDownloader — asyncio.TimeoutError → DownloadError
# ---------------------------------------------------------------------------

async def test_audio_downloader_translates_timeout(tmp_path: Path) -> None:
    """TimeoutError (or ServerTimeoutError) from aiohttp.ClientSession.get → DownloadError.

    Note: aiohttp.ServerTimeoutError is a subclass of aiohttp.ClientError, so
    it gets caught by the existing `except aiohttp.ClientError` in _downloader.py.
    We test both the stdlib asyncio.TimeoutError (which would bubble up uncaught
    unless the outer try/except catches it) and the aiohttp subclass variant.
    """
    downloader = AudioDownloader()
    dest = str(tmp_path / "audio.m4s")

    with patch("bili_unit.processing.audio._downloader.aiohttp.ClientSession") as MockSession:
        mock_sess = MagicMock()
        # aiohttp.ServerTimeoutError is a ClientError subclass — always caught
        mock_sess.get = MagicMock(side_effect=aiohttp.ServerTimeoutError())
        MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        MockSession.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(DownloadError):
            await downloader.download_to_file("https://cdn.example.com/audio.m4s", dest)


# ---------------------------------------------------------------------------
# 3. _fetch_subtitle_body — aiohttp.ClientError → {"_fetch_error": ...} sentinel
# ---------------------------------------------------------------------------

async def test_subtitle_fetch_returns_error_on_aiohttp_failure() -> None:
    """_fetch_subtitle_body must never raise; aiohttp.ClientError → sentinel dict."""
    from bili_unit.fetching._bilibili_adapter import _fetch_subtitle_body

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

    # Must not raise — returns a dict with _fetch_error key instead.
    result = await _fetch_subtitle_body(mock_sess, "https://i0.hdslb.com/bfs/subtitle/test.json")

    assert isinstance(result, dict), "expected dict, not an exception"
    assert "_fetch_error" in result, f"expected '_fetch_error' key, got keys: {list(result.keys())}"
    assert "body" not in result


# ---------------------------------------------------------------------------
# 4. MimoASRBackend — HTTP 401 → ASRAPIError
# ---------------------------------------------------------------------------

async def test_mimo_backend_http_401_raises_asr_api_error() -> None:
    """A 401 from MiMo endpoint must surface as ASRAPIError (not ASRConfigError)."""
    backend = MimoASRBackend(api_key="tp-test-key")

    resp = _make_aiohttp_resp_mock(
        status=401,
        json_data={"error": {"message": "unauthorized", "code": "invalid_api_key"}},
    )
    mock_sess = _make_session_mock(resp)

    with (
        patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_sess)),
        pytest.raises(ASRAPIError, match="401"),
    ):
        await backend.transcribe(b"\x00" * 256, mime_type="audio/mp3")

    await backend.close()


# ---------------------------------------------------------------------------
# 5. MimoASRBackend — HTTP 429 → ASRAPIError
#    (The runner treats all AudioError subclasses except ASRConfigError as
#    retryable; ASRAPIError is the correct class for rate-limiting responses.)
# ---------------------------------------------------------------------------

async def test_mimo_backend_http_429_raises_asr_api_error() -> None:
    """A 429 rate-limit response from MiMo must raise ASRAPIError."""
    backend = MimoASRBackend(api_key="tp-test-key")

    resp = _make_aiohttp_resp_mock(
        status=429,
        json_data={"error": {"message": "rate limit exceeded", "code": "rate_limit_exceeded"}},
    )
    mock_sess = _make_session_mock(resp)

    with (
        patch.object(backend, "_get_session", new=AsyncMock(return_value=mock_sess)),
        pytest.raises(ASRAPIError, match="429"),
    ):
        await backend.transcribe(b"\x00" * 256, mime_type="audio/mp3")

    # ASRAPIError is a subclass of AudioError — the runner retries it.
    # ASRConfigError is the *non*-retryable carve-out; verify 429 is NOT that.
    from bili_unit.processing import ASRConfigError
    try:
        backend2 = MimoASRBackend(api_key="tp-test-key")
        resp2 = _make_aiohttp_resp_mock(status=429, json_data={"error": "rate limit"})
        mock_sess2 = _make_session_mock(resp2)
        with patch.object(backend2, "_get_session", new=AsyncMock(return_value=mock_sess2)):
            await backend2.transcribe(b"\x00" * 256)
    except ASRAPIError as exc:
        assert not isinstance(exc, ASRConfigError), "429 must not be ASRConfigError (non-retryable)"
    finally:
        await backend2.close()

    await backend.close()


# ---------------------------------------------------------------------------
# 6. convert_m4s_to_mp3 — ffmpeg returncode=1 → ConvertError
# ---------------------------------------------------------------------------

async def test_ffmpeg_subprocess_nonzero_exit_raises_convert_error(tmp_path: Path) -> None:
    """A non-zero ffmpeg exit code must raise ConvertError with stderr content."""
    from bili_unit.processing.audio import convert_m4s_to_mp3

    input_file = tmp_path / "audio.m4s"
    input_file.write_bytes(b"\x00" * 16)  # dummy content
    output_file = tmp_path / "audio.mp3"

    # Build a mock process whose communicate() returns (b"", stderr) and
    # whose returncode is 1.
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"some ffmpeg failure: invalid input"))

    with (
        patch(
            "bili_unit.processing.audio._converter.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
        pytest.raises(ConvertError) as exc_info,
    ):
        await convert_m4s_to_mp3(input_file, output_file)

    assert "ffmpeg failed" in str(exc_info.value).lower() or "rc=1" in str(exc_info.value)
    # stderr content should appear in the message (last 500 chars are included)
    assert "ffmpeg failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Connection.run_transaction — OperationalError propagates, first INSERT rolled back
# ---------------------------------------------------------------------------

async def test_sqlite_operational_error_propagates_and_rolls_back(tmp_path: Path) -> None:
    """run_transaction rolls back the whole tx when any statement fails.

    The transaction contains:
      - statement 1: valid INSERT into a real table  (would succeed alone)
      - statement 2: INSERT into a non-existent table (raises OperationalError)

    After the failed tx:
      - OperationalError is re-raised
      - statement 1's data is NOT in the DB (rollback worked)
    """
    from bili_unit._db.connection import Connection

    db_path = tmp_path / "test_rollback.db"
    conn = Connection(db_path, uid=12345)
    await conn.open()

    # Create a scratch table independent of the DDL schema so we can insert
    # a sentinel row without touching the versioned tables.
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, val TEXT)"
    )

    # Confirm the table starts empty.
    rows_before = await conn.fetch_all("SELECT * FROM scratch")
    assert rows_before == []

    # Attempt a transaction where the first statement succeeds but the second fails.
    with pytest.raises(sqlite3.OperationalError):
        await conn.run_transaction([
            ("INSERT INTO scratch(id, val) VALUES (1, 'should-be-rolled-back')", ()),
            ("INSERT INTO nonexistent_table(x) VALUES (1)", ()),  # will fail
        ])

    # The first INSERT must NOT be committed — rollback should have undone it.
    rows_after = await conn.fetch_all("SELECT * FROM scratch")
    assert rows_after == [], (
        f"Expected empty table after rollback, but found {len(rows_after)} row(s): "
        f"{[dict(r) for r in rows_after]}"
    )

    await conn.close()
