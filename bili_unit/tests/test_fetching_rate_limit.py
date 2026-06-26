# tests for bili_unit/fetching/rate_limit
# Run: uv run pytest bili_unit/tests/test_rate_limit.py -v

import pytest

from bili_unit.fetching._endpoint_catalog import ENDPOINTS
from bili_unit.fetching.rate_limit import _ITEM_FANOUT_ENDPOINTS, RateLimitController


def test_all_item_endpoints_use_fanout_rate_limit_bucket():
    item_endpoints = {endpoint.name for endpoint in ENDPOINTS if endpoint.kind == "item"}

    assert item_endpoints <= _ITEM_FANOUT_ENDPOINTS


@pytest.mark.asyncio
async def test_rate_limit_acquire_passes():
    rl = RateLimitController(global_qps=100, endpoint_qps=100)
    await rl.acquire("test")
    # no exception = success


@pytest.mark.asyncio
async def test_rate_limit_acquire_different_endpoints():
    rl = RateLimitController(global_qps=100, endpoint_qps=100)
    await rl.acquire("a")
    await rl.acquire("b")
    await rl.acquire("a")


@pytest.mark.asyncio
async def test_rate_limit_412_adjusts_global_qps():
    rl = RateLimitController(global_qps=1.0, endpoint_qps=0.5)
    advice = await rl.record_412("videos")
    assert advice["global_qps"] <= 0.5  # halved from 1.0
    assert advice["wait_seconds"] == 30.0
    assert advice["paused_until"] is not None


@pytest.mark.asyncio
async def test_rate_limit_412_adjusts_endpoint_qps():
    rl = RateLimitController(global_qps=10, endpoint_qps=1.0)
    # first acquire to create the endpoint limiter
    await rl.acquire("videos")
    advice = await rl.record_412("videos")
    assert advice["endpoint_qps"] <= 0.5  # halved from 1.0


@pytest.mark.asyncio
async def test_rate_limit_412_floor_qps():
    """QPS never drops below 0.05."""
    rl = RateLimitController(global_qps=0.05, endpoint_qps=0.05)
    await rl.acquire("x")
    advice = await rl.record_412("x")
    assert advice["global_qps"] >= 0.05
    assert advice["endpoint_qps"] >= 0.05


@pytest.mark.asyncio
async def test_rate_limit_does_not_modify_task():
    """rate_limit has no knowledge of task state."""
    rl = RateLimitController()
    assert not hasattr(rl, "task")
    assert not hasattr(rl, "_task")
    assert not hasattr(rl, "update_task")


# ======================================================================
# QPS recovery (cooldown-based recovery after 412)
# ======================================================================


@pytest.mark.asyncio
async def test_recovery_no_412_no_change():
    """QPS stays at original when no 412 has occurred."""
    rl = RateLimitController(global_qps=1.0, endpoint_qps=0.5, recovery_cooldown=10)
    await rl.acquire("test")
    assert rl._global_qps == 1.0
    assert rl._endpoint_qps == 0.5


@pytest.mark.asyncio
async def test_recovery_before_cooldown_no_change():
    """QPS does NOT recover before cooldown period elapses."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("videos")
    await rl.record_412("videos")
    assert rl._global_qps == 0.5

    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("videos")
    # 200s < 300s cooldown → no recovery
    with patch.object(rl_mod.time, "time", return_value=300.0):
        await rl.acquire("videos")
    assert rl._global_qps == 0.25  # halved again, no recovery


@pytest.mark.asyncio
async def test_recovery_after_cooldown_doubles_qps():
    """After cooldown without 412, QPS doubles (capped at original)."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("videos")
    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("videos")
    assert rl._global_qps == 0.5

    # 500s > 100 + 300 cooldown → recovery: 0.5 * 2 = 1.0 (capped at original)
    with patch.object(rl_mod.time, "time", return_value=500.0):
        await rl.acquire("videos")
    assert rl._global_qps == 1.0


@pytest.mark.asyncio
async def test_recovery_two_cooldown_periods():
    """Two consecutive cooldown periods restore more QPS."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=0.8,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("videos")

    # First 412: 0.8 → 0.4
    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("videos")
    assert rl._global_qps == 0.4

    # Second 412 within cooldown: 0.4 → 0.2
    with patch.object(rl_mod.time, "time", return_value=200.0):
        await rl.record_412("videos")
    assert rl._global_qps == 0.2

    # First recovery at 600s (200 + 300 + 100): 0.2 * 2 = 0.4
    with patch.object(rl_mod.time, "time", return_value=600.0):
        await rl.acquire("videos")
    assert rl._global_qps == 0.4

    # Second recovery at 950s (600 + 300 + 50): 0.4 * 2 = 0.8
    with patch.object(rl_mod.time, "time", return_value=950.0):
        await rl.acquire("videos")
    assert rl._global_qps == 0.8


@pytest.mark.asyncio
async def test_recovery_412_resets_cooldown():
    """A new 412 during recovery resets the cooldown timer."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("videos")

    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("videos")
    assert rl._global_qps == 0.5

    # Recovery at 500s: 0.5 * 2 = 1.0
    with patch.object(rl_mod.time, "time", return_value=500.0):
        await rl.acquire("videos")
    assert rl._global_qps == 1.0

    # New 412 at 600s: 1.0 → 0.5
    with patch.object(rl_mod.time, "time", return_value=600.0):
        await rl.record_412("videos")
    assert rl._global_qps == 0.5

    # 800s is only 200s after last 412 (600s) → no recovery yet
    with patch.object(rl_mod.time, "time", return_value=800.0):
        await rl.acquire("videos")
    assert rl._global_qps == 0.5

    # 950s > 600 + 300 → recovery: 0.5 * 2 = 1.0
    with patch.object(rl_mod.time, "time", return_value=950.0):
        await rl.acquire("videos")
    assert rl._global_qps == 1.0


@pytest.mark.asyncio
async def test_recovery_capped_at_original():
    """QPS never exceeds original value."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("test")
    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("test")
    assert rl._global_qps == 0.5

    # Recovery: 0.5 * 2 = 1.0, capped at original 1.0
    with patch.object(rl_mod.time, "time", return_value=500.0):
        await rl.acquire("test")
    assert rl._global_qps == 1.0

    # Another cooldown: should stay at 1.0, not exceed
    with patch.object(rl_mod.time, "time", return_value=900.0):
        await rl.acquire("test")
    assert rl._global_qps == 1.0


@pytest.mark.asyncio
async def test_recovery_video_detail_independent():
    """video_detail QPS recovers independently from its own 412s."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=10.0,
        endpoint_qps=1.0,
        video_detail_qps=0.4,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("video_detail")
    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("video_detail")
    assert rl._video_detail_qps == 0.2

    # Recovery: 0.2 * 2 = 0.4
    with patch.object(rl_mod.time, "time", return_value=500.0):
        await rl.acquire("video_detail")
    assert rl._video_detail_qps == 0.4


@pytest.mark.asyncio
async def test_recovery_multiple_412s_gradual_recovery():
    """Multiple rapid 412s followed by gradual recovery over several cooldowns."""
    from unittest.mock import patch

    import bili_unit.fetching.rate_limit as rl_mod

    rl = RateLimitController(
        global_qps=1.0,
        endpoint_qps=0.5,
        recovery_cooldown=300,
        pause_seconds=0,
    )
    await rl.acquire("test")

    # Three rapid 412s: 1.0 → 0.5 → 0.25 → 0.125
    with patch.object(rl_mod.time, "time", return_value=100.0):
        await rl.record_412("test")
    with patch.object(rl_mod.time, "time", return_value=150.0):
        await rl.record_412("test")
    with patch.object(rl_mod.time, "time", return_value=200.0):
        await rl.record_412("test")
    assert rl._global_qps == 0.125

    # Recovery 1 at 600s (200+300+100): 0.125 * 2 = 0.25
    with patch.object(rl_mod.time, "time", return_value=600.0):
        await rl.acquire("test")
    assert rl._global_qps == 0.25

    # Recovery 2 at 950s: 0.25 * 2 = 0.5
    with patch.object(rl_mod.time, "time", return_value=950.0):
        await rl.acquire("test")
    assert rl._global_qps == 0.5

    # Recovery 3 at 1300s: 0.5 * 2 = 1.0 (capped at original)
    with patch.object(rl_mod.time, "time", return_value=1300.0):
        await rl.acquire("test")
    assert rl._global_qps == 1.0
