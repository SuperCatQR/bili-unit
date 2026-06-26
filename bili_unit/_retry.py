# Generic retry driver shared by fetching and processing layers.
#
# Replaces four bespoke retry loops (fetching uid-level / item-level,
# processing transform / audio).  Each caller supplies:
#   * a classifier — exc → RETRYABLE | PERMANENT
#   * an optional on_attempt_failed callback — runs after every failure
#     to record errors / update status, and may override the next sleep
#     duration (used for 412 advice ``wait_seconds``).
#
# Semantics:
#   max_attempts = N means at most N attempts in total (1 + N-1 retries).
#   Existing call-sites use ``max_retries`` (number of retries excluding
#   the initial attempt), so they pass ``max_attempts=settings.max_retries+1``.
#   This keeps the RetryPolicy field unambiguous — ``max_attempts`` always
#   counts the initial try.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

logger = logging.getLogger("bili.retry")

T = TypeVar("T")


def parse_retry_delays(raw: str, default: list[int] | None = None) -> list[int]:
    """Parse a comma-separated env string into a sorted list of seconds.

    Falls back to ``default`` (or ``[30, 60, 120]``) on parse failure or
    empty input.  Sorting matches the existing ``processing.env`` behaviour
    so a misconfigured env never produces a backoff that shrinks.
    """
    fallback = default if default is not None else [30, 60, 120]
    try:
        delays = [int(s.strip()) for s in raw.split(",") if s.strip()]
    except ValueError:
        return list(fallback)
    if not delays:
        return list(fallback)
    return sorted(delays)


class RetryClassification(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"  # terminate immediately; caller decides downstream effect


@dataclass
class RetryOutcome:
    """One attempt's outcome, surfaced to ``on_attempt_failed``."""

    attempt: int  # 1-based
    will_retry: bool  # driver has already decided whether to sleep + try again
    classification: RetryClassification
    delay_seconds: int  # default sleep before next attempt; meaningful only when will_retry


@dataclass
class RetryPolicy:
    max_attempts: int  # total attempts including the initial try (>= 1)
    delays: list[int]
    classify: Callable[[Exception], RetryClassification]


class RetryDriver:
    """Drive a retryable operation under a :class:`RetryPolicy`.

    ``run`` returns the operation's value on success.  On exhaustion or a
    ``PERMANENT`` classification, the last exception is re-raised — but only
    after ``on_attempt_failed`` (if provided) has run for that final failure,
    so callers can record the error / write final status before the raise.

    ``on_attempt_failed`` may return an ``int`` to override the sleep delay
    before the next retry (e.g. a 412 advisory wait).  ``None`` keeps the
    policy default.
    """

    def __init__(self, policy: RetryPolicy) -> None:
        if policy.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._p = policy

    async def run(
        self,
        op: Callable[[], Awaitable[T]],
        *,
        on_attempt_failed: Callable[[Exception, RetryOutcome], Awaitable[int | None]] | None = None,
    ) -> T:
        # Note: catch ``Exception`` (not ``BaseException``) so KeyboardInterrupt
        # / SystemExit propagate without spuriously triggering the failure
        # callback or writing FAILED_PERMANENT status.  Matches the four
        # original retry loops this driver replaces.
        last_exc: Exception | None = None
        for attempt in range(1, self._p.max_attempts + 1):
            try:
                return await op()
            except Exception as exc:  # noqa: BLE001 — surface to caller via callback
                last_exc = exc
                cls = self._p.classify(exc)
                if cls == RetryClassification.PERMANENT:
                    outcome = RetryOutcome(
                        attempt=attempt,
                        will_retry=False,
                        classification=cls,
                        delay_seconds=0,
                    )
                    if on_attempt_failed is not None:
                        await on_attempt_failed(exc, outcome)
                    raise

                # RETRYABLE
                will_retry = attempt < self._p.max_attempts
                default_delay = self._p.delays[min(attempt - 1, len(self._p.delays) - 1)] if self._p.delays else 0
                outcome = RetryOutcome(
                    attempt=attempt,
                    will_retry=will_retry,
                    classification=cls,
                    delay_seconds=default_delay,
                )
                override: int | None = None
                if on_attempt_failed is not None:
                    override = await on_attempt_failed(exc, outcome)
                if not will_retry:
                    raise
                wait = override if override is not None else default_delay
                await asyncio.sleep(max(0, wait))
        # Defensive: the loop above always either returns or raises.
        assert last_exc is not None  # pragma: no cover
        raise last_exc  # pragma: no cover
