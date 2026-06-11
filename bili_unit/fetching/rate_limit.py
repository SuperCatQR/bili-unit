# rate_limit — global + per-endpoint rate limiting via aiolimiter.
# QPS recovery: after 412-triggered reduction, QPS doubles back toward original
# after each cooldown period without further 412s.

import asyncio
import logging
import time
from typing import Any

from aiolimiter import AsyncLimiter

from . import RateLimitError  # noqa: F401 — reserved for future rate-limit pause/resume

logger = logging.getLogger("bili.fetching.rate_limit")


class RateLimitController:
    """Provides per-endpoint and global rate-limit gates with QPS recovery.

    Does NOT orchestrate retries — that is the runner's responsibility.
    record_412() adjusts limiters and returns *advice* (e.g. suggested wait).

    Recovery behaviour:
      After a 412, QPS is halved (floor 0.05).  Each subsequent
      ``recovery_cooldown`` seconds without any 412 doubles the current
      QPS back toward the original (constructor-supplied) value.
    """

    def __init__(
        self,
        global_qps: float = 0.5,
        endpoint_qps: float = 0.2,
        video_detail_qps: float = 0.2,
        pause_seconds: float = 30.0,
        recovery_cooldown: float = 300.0,
    ) -> None:
        # aiolimiter default acquire(amount=1) requires max_rate >= 1.
        # Scale up: use time_period=10 and max_rate = round(qps * 10).
        self._global_qps = global_qps
        self._endpoint_qps = endpoint_qps
        self._video_detail_qps = video_detail_qps
        # Original (ceiling) values — recovery never exceeds these.
        self._orig_global_qps = global_qps
        self._orig_endpoint_qps = endpoint_qps
        self._orig_video_detail_qps = video_detail_qps
        self._pause_seconds = pause_seconds
        self._recovery_cooldown = recovery_cooldown
        self._global: AsyncLimiter = AsyncLimiter(
            max_rate=max(round(global_qps * 10), 1), time_period=10,
        )
        self._endpoint_limiters: dict[str, AsyncLimiter] = {}
        self._lock = asyncio.Lock()
        self._paused_until: float | None = None
        self._last_412_at: float | None = None
        self._recovered_until: float | None = None

    # -- acquire ------------------------------------------------------------

    async def acquire(self, endpoint: str) -> None:
        """Wait for both global and endpoint rate-limit permits."""
        # respect 412 pause
        if self._paused_until is not None:
            remaining = self._paused_until - time.time()
            if remaining > 0:
                logger.info("rate_limit_paused", extra={"wait_s": round(remaining, 1)})
                await asyncio.sleep(remaining)

        # QPS recovery check (before every request)
        self._try_recover()

        # global gate
        await self._global.acquire()

        # endpoint gate — lazy-create
        ep_limiter = self._endpoint_limiters.get(endpoint)
        if ep_limiter is None:
            async with self._lock:
                ep_limiter = self._endpoint_limiters.get(endpoint)
                if ep_limiter is None:
                    qps = self._video_detail_qps if endpoint == "video_detail" else self._endpoint_qps
                    ep_limiter = AsyncLimiter(
                        max_rate=max(round(qps * 10), 1),
                        time_period=10,
                    )
                    self._endpoint_limiters[endpoint] = ep_limiter
        await ep_limiter.acquire()

    # -- QPS recovery -------------------------------------------------------

    @staticmethod
    def _recover_step(current: float, original: float) -> float:
        """Double current QPS, capped at original.

        Returns *original* when the result is within 5 % of it, to avoid
        lingering near-original floating-point values.
        """
        if current >= original:
            return original
        new = min(current * 2, original)
        if new >= original * 0.95:
            return original
        return new

    def _try_recover(self) -> None:
        """Attempt QPS recovery if cooldown has elapsed since last 412.

        Called inside ``acquire()`` before each request.  Non-blocking —
        only mutates state when recovery is actually due.
        """
        if self._last_412_at is None:
            return

        now = time.time()
        if now - self._last_412_at < self._recovery_cooldown:
            return

        # Determine the effective recovery epoch: the later of
        # (last_412_at + cooldown) and (last recovery step + cooldown).
        epoch = self._last_412_at + self._recovery_cooldown
        if self._recovered_until is not None:
            epoch = max(epoch, self._recovered_until)

        if now < epoch:
            return

        # --- apply recovery (synchronous, no lock needed) ---
        global_changed = False
        if self._global_qps < self._orig_global_qps:
            self._global_qps = self._recover_step(self._global_qps, self._orig_global_qps)
            self._global = AsyncLimiter(
                max_rate=max(round(self._global_qps * 10), 1), time_period=10,
            )
            global_changed = True

        ep_changed = False
        if self._endpoint_qps < self._orig_endpoint_qps:
            self._endpoint_qps = self._recover_step(self._endpoint_qps, self._orig_endpoint_qps)
            ep_changed = True

        vd_changed = False
        if self._video_detail_qps < self._orig_video_detail_qps:
            self._video_detail_qps = self._recover_step(self._video_detail_qps, self._orig_video_detail_qps)
            vd_changed = True

        if ep_changed or vd_changed:
            for ep_name, _limiter in list(self._endpoint_limiters.items()):
                if ep_name == "video_detail":
                    if vd_changed:
                        self._endpoint_limiters[ep_name] = AsyncLimiter(
                            max_rate=max(round(self._video_detail_qps * 10), 1), time_period=10,
                        )
                else:
                    if ep_changed:
                        self._endpoint_limiters[ep_name] = AsyncLimiter(
                            max_rate=max(round(self._endpoint_qps * 10), 1), time_period=10,
                        )

        self._recovered_until = epoch + self._recovery_cooldown

        if global_changed or ep_changed or vd_changed:
            logger.info(
                "qps_recovered",
                extra={
                    "global_qps": self._global_qps,
                    "endpoint_qps": self._endpoint_qps,
                    "video_detail_qps": self._video_detail_qps,
                },
            )

    # -- 412 handling -------------------------------------------------------

    async def record_412(self, endpoint: str) -> dict[str, Any]:
        """Called when a 412 is received.  Adjusts limiters and returns advice."""
        now = time.time()
        async with self._lock:
            self._last_412_at = now
            # Reset recovery state — any new 412 restarts the cooldown.
            self._recovered_until = None

            # halve global QPS (floor 0.05)
            new_global = max(0.05, self._global_qps / 2)
            self._global_qps = new_global
            self._global = AsyncLimiter(
                max_rate=max(round(new_global * 10), 1), time_period=10,
            )

            # halve endpoint QPS if limiter exists
            ep = self._endpoint_limiters.get(endpoint)
            if ep:
                if endpoint == "video_detail":
                    new_ep = max(0.05, self._video_detail_qps / 2)
                    self._video_detail_qps = new_ep
                else:
                    new_ep = max(0.05, self._endpoint_qps / 2)
                    self._endpoint_qps = new_ep
                self._endpoint_limiters[endpoint] = AsyncLimiter(
                    max_rate=max(round(new_ep * 10), 1), time_period=10,
                )

            # pause
            self._paused_until = now + self._pause_seconds

        return {
            "paused_until": self._paused_until,
            "global_qps": self._global_qps,
            "endpoint_qps": self._endpoint_qps,
            "wait_seconds": self._pause_seconds,
        }

    @property
    def paused_until(self) -> float | None:
        return self._paused_until

    def to_state(self, endpoint: str | None = None) -> dict[str, Any]:
        """Serialise current rate-limit state for storage."""
        if endpoint is None:
            qps = self._global_qps
        elif endpoint == "video_detail":
            qps = self._video_detail_qps
        else:
            qps = self._endpoint_qps

        state: dict[str, Any] = {
            "scope": "global" if endpoint is None else endpoint,
            "endpoint": endpoint,
            "qps": qps,
            "paused_until": self._paused_until,
            "last_412_at": self._last_412_at,
            "updated_at": int(time.time() * 1000),
        }
        # Include original values for monitoring / debugging.
        if endpoint is None:
            state["original_global_qps"] = self._orig_global_qps
        elif endpoint == "video_detail":
            state["original_endpoint_qps"] = self._orig_video_detail_qps
        else:
            state["original_endpoint_qps"] = self._orig_endpoint_qps
        return state
