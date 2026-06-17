# tests for bili_unit._retry — generic RetryDriver semantics.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bili_unit._retry import (
    RetryClassification,
    RetryDriver,
    RetryOutcome,
    RetryPolicy,
    parse_retry_delays,
)


def _retryable(_exc: Exception) -> RetryClassification:
    return RetryClassification.RETRYABLE


def _permanent(_exc: Exception) -> RetryClassification:
    return RetryClassification.PERMANENT


# ----- parse_retry_delays --------------------------------------------------


def test_parse_retry_delays_normal():
    assert parse_retry_delays("30,60,120") == [30, 60, 120]


def test_parse_retry_delays_unsorted_input_is_sorted():
    assert parse_retry_delays("60,30,120") == [30, 60, 120]


def test_parse_retry_delays_empty_falls_back():
    assert parse_retry_delays("") == [30, 60, 120]


def test_parse_retry_delays_invalid_falls_back():
    assert parse_retry_delays("a,b") == [30, 60, 120]


def test_parse_retry_delays_custom_default():
    assert parse_retry_delays("", default=[1, 2]) == [1, 2]


# ----- RetryDriver basics --------------------------------------------------


@pytest.mark.asyncio
async def test_succeeds_on_first_attempt():
    op = AsyncMock(return_value="ok")
    driver = RetryDriver(RetryPolicy(
        max_attempts=3, delays=[0, 0], classify=_retryable,
    ))
    result = await driver.run(op)
    assert result == "ok"
    assert op.call_count == 1


@pytest.mark.asyncio
async def test_succeeds_on_third_attempt_before_exhaustion():
    calls = [0]

    async def op():
        calls[0] += 1
        if calls[0] < 3:
            raise RuntimeError(f"fail{calls[0]}")
        return "yay"

    seen: list[RetryOutcome] = []

    async def cb(_exc, outcome):
        seen.append(outcome)
        return None

    driver = RetryDriver(RetryPolicy(
        max_attempts=4, delays=[0, 0, 0], classify=_retryable,
    ))
    with patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()):
        result = await driver.run(op, on_attempt_failed=cb)
    assert result == "yay"
    assert calls[0] == 3
    # Two failures recorded; both will_retry=True.
    assert len(seen) == 2
    assert all(o.will_retry for o in seen)


@pytest.mark.asyncio
async def test_exhausts_then_raises_last_exception():
    async def op():
        raise RuntimeError("boom")

    seen_will_retry = []

    async def cb(_exc, outcome):
        seen_will_retry.append(outcome.will_retry)
        return None

    driver = RetryDriver(RetryPolicy(
        max_attempts=3, delays=[0, 0], classify=_retryable,
    ))
    with (
        patch("bili_unit._retry.asyncio.sleep", new=AsyncMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await driver.run(op, on_attempt_failed=cb)
    # Three attempts ⇒ three callback invocations; only the last has will_retry=False.
    assert seen_will_retry == [True, True, False]


@pytest.mark.asyncio
async def test_permanent_classification_terminates_immediately():
    calls = [0]

    async def op():
        calls[0] += 1
        raise ValueError("nope")

    final_outcome = []

    async def cb(_exc, outcome):
        final_outcome.append(outcome)
        return None

    driver = RetryDriver(RetryPolicy(
        max_attempts=4, delays=[0, 0, 0], classify=_permanent,
    ))
    with pytest.raises(ValueError, match="nope"):
        await driver.run(op, on_attempt_failed=cb)
    # Single attempt, single callback, will_retry=False.
    assert calls[0] == 1
    assert len(final_outcome) == 1
    assert final_outcome[0].will_retry is False
    assert final_outcome[0].classification == RetryClassification.PERMANENT


@pytest.mark.asyncio
async def test_callback_override_replaces_default_delay():
    """on_attempt_failed returning an int wins over policy delay."""
    calls = [0]

    async def op():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("first")
        return "fine"

    sleep_mock = AsyncMock()

    async def cb(_exc, outcome):
        # Default delay would be 5; we override to 99.
        assert outcome.delay_seconds == 5
        return 99

    driver = RetryDriver(RetryPolicy(
        max_attempts=3, delays=[5, 5], classify=_retryable,
    ))
    with patch("bili_unit._retry.asyncio.sleep", sleep_mock):
        result = await driver.run(op, on_attempt_failed=cb)

    assert result == "fine"
    sleep_mock.assert_awaited_once_with(99)


@pytest.mark.asyncio
async def test_callback_none_keeps_default_delay():
    calls = [0]

    async def op():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("first")
        return "fine"

    sleep_mock = AsyncMock()

    async def cb(_exc, _outcome):
        return None

    driver = RetryDriver(RetryPolicy(
        max_attempts=2, delays=[7], classify=_retryable,
    ))
    with patch("bili_unit._retry.asyncio.sleep", sleep_mock):
        await driver.run(op, on_attempt_failed=cb)
    sleep_mock.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_max_attempts_one_is_no_retry():
    """max_attempts=1 means only the initial attempt — no retry."""
    calls = [0]

    async def op():
        calls[0] += 1
        raise RuntimeError("once")

    driver = RetryDriver(RetryPolicy(
        max_attempts=1, delays=[1], classify=_retryable,
    ))
    with pytest.raises(RuntimeError):
        await driver.run(op)
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_invalid_max_attempts_rejected():
    with pytest.raises(ValueError):
        RetryDriver(RetryPolicy(max_attempts=0, delays=[], classify=_retryable))


@pytest.mark.asyncio
async def test_delay_clamps_at_last_value():
    """When attempts > delay list, last delay is reused."""
    calls = [0]

    async def op():
        calls[0] += 1
        if calls[0] < 4:
            raise RuntimeError("retry me")
        return "ok"

    sleep_seen: list[float | int] = []

    async def fake_sleep(s):
        sleep_seen.append(s)

    driver = RetryDriver(RetryPolicy(
        max_attempts=5, delays=[1, 2], classify=_retryable,
    ))
    with patch("bili_unit._retry.asyncio.sleep", side_effect=fake_sleep):
        result = await driver.run(op)
    assert result == "ok"
    # 3 failures → 3 sleeps. Last delay (2) should clamp.
    assert sleep_seen == [1, 2, 2]
