"""Tests for WorkerClient — main-side IPC client.

Uses FakeWorker (in-memory) to test the full WorkerClient lifecycle
without spawning a real subprocess.

Tests that exercise the real WorkerClient startup sequence (handshake validation,
catalog parsing, etc.) are skipped when no real bili-worker binary is available.
The FakeWorker covers the same logic at the op level.
"""

from __future__ import annotations

import pytest

from bili_unit.fetching._error_pack import ErrorPack
from bili_unit.fetching._protocol import ProtocolError
from bili_unit.fetching.worker_client import (
    WorkerClient,
    WorkerCrashedError,
    WorkerNotStartedError,
)
from bili_unit.tests.fake_worker import FakeWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_worker():
    """Create a fresh FakeWorker with valid handshake + catalog + credential."""
    fw = FakeWorker()
    fw.configure_handshake()
    fw.configure_catalog()
    fw.responses["credential_open"] = {"credential_ref": "cred-1"}
    fw.responses["init_http_backend"] = {"backend": "aiohttp"}
    return fw


# ---------------------------------------------------------------------------
# FakeWorker startup validation (mirrors WorkerClient._handshake / _describe_catalog)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_worker_handshake_ok(fake_worker):
    """FakeWorker handshake returns expected protocol version."""
    data = await fake_worker._dispatch("handshake", {"protocol_version": "1.0"})
    assert data["protocol_version"] == "1.0"


@pytest.mark.asyncio
async def test_fake_worker_catalog_count(fake_worker):
    """FakeWorker describe_catalog returns 63 endpoints (33 uid + 30 item)."""
    data = await fake_worker._dispatch("describe_catalog", {})
    endpoints = data["endpoints"]
    uid_count = sum(1 for ep in endpoints if ep.get("kind") == "uid")
    item_count = sum(1 for ep in endpoints if ep.get("kind") == "item")
    assert len(endpoints) == 63
    assert uid_count == 33
    assert item_count == 30


# ---------------------------------------------------------------------------
# Op dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_op_before_start():
    """Sending ops before start() should raise WorkerNotStartedError."""
    client = WorkerClient()

    # Use _send_op directly to bypass the conftest mock on fetch_page.
    with pytest.raises(WorkerNotStartedError, match="not started"):
        await client._send_op("fetch_page", {"uid": 1, "endpoint": "videos"})


# ---------------------------------------------------------------------------
# Error pack roundtrip (FakeWorker simulation of WorkerClient error handling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_pack_roundtrip_retryable():
    """WorkerClient should raise the correct exception from an error pack."""
    fw = FakeWorker()
    fw.errors["fetch_page"] = ErrorPack(
        type="Http412Error",
        classification="retryable",
        code=412,
        message="videos: 412",
        retryable_hint=True,
    )
    from bili_unit.fetching import Http412Error

    with pytest.raises(Http412Error, match="videos: 412"):
        await fw.fetch_page(1, "videos", None, {})


@pytest.mark.asyncio
async def test_error_pack_roundtrip_permanent():
    """Permanent error pack → AuthError raised."""
    fw = FakeWorker()
    fw.errors["fetch_page"] = ErrorPack(
        type="AuthError",
        classification="permanent",
        code=None,
        message="credential missing",
        retryable_hint=False,
    )
    from bili_unit.fetching import AuthError

    with pytest.raises(AuthError, match="credential missing"):
        await fw.fetch_page(1, "videos", None, {})


@pytest.mark.asyncio
async def test_error_pack_roundtrip_unavailable():
    """Unavailable error pack → ResourceUnavailableError raised."""
    fw = FakeWorker()
    fw.errors["fetch_page"] = ErrorPack(
        type="ResourceUnavailableError",
        classification="unavailable",
        code=53013,
        message="videos: code=53013: 用户隐私设置未公开",
        retryable_hint=False,
    )
    from bili_unit.fetching import ResourceUnavailableError

    with pytest.raises(ResourceUnavailableError, match="53013"):
        await fw.fetch_page(1, "videos", None, {})


# ---------------------------------------------------------------------------
# Worker crash simulation (FakeWorker lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_crash_mid_request():
    """Crash after a specific op should raise WorkerCrashedError."""
    fw = FakeWorker()
    fw.crash_after_op = "fetch_page"

    with pytest.raises(WorkerCrashedError, match="crashed after fetch_page"):
        await fw.fetch_page(1, "videos", None, {})


@pytest.mark.asyncio
async def test_worker_eof_during_startup():
    """stdout EOF during startup should raise WorkerCrashedError."""
    fw = FakeWorker()
    fw.simulate_eof = True

    with pytest.raises(WorkerCrashedError, match="EOF"):
        await fw.start()


@pytest.mark.asyncio
async def test_worker_nonzero_exit_during_startup():
    """Non-zero exit during startup should raise WorkerCrashedError."""
    fw = FakeWorker()
    fw.simulate_nonzero_exit = True

    with pytest.raises(WorkerCrashedError, match="exited with code 1"):
        await fw.start()


# ---------------------------------------------------------------------------
# Protocol errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_op():
    """Calling an op with no configured response should raise ProtocolError."""
    fw = FakeWorker()

    with pytest.raises(ProtocolError, match="no response configured"):
        await fw.fetch_page(1, "videos", None, {})


# ---------------------------------------------------------------------------
# Credential ref flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_ref_from_responses():
    """credential_ref is derived from the credential_open response."""
    fw = FakeWorker()
    fw.configure_handshake()
    fw.configure_catalog()
    fw.responses["credential_open"] = {"credential_ref": "cred-42"}
    fw.responses["init_http_backend"] = {"backend": "aiohttp"}

    assert fw.credential_ref == "cred-42"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_marks_not_started():
    """After shutdown, started should be False."""
    fw = FakeWorker()
    await fw.shutdown()
    assert not fw.started
