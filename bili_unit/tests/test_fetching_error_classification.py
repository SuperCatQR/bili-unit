# tests for bili_unit/fetching/_bilibili_adapter error-classification logic
# Run: uv run pytest bili_unit/tests/test_fetching_error_classification.py -v
"""Three slow-fetch fixes locked in by these tests:

1. ``fetch_user_channels`` keyword convention — call site uses ``cred=...``,
   so the function signature must accept ``cred`` (not ``credential``);
   regression would silently leave the credential unbound and raise
   ``RequestError: missing 1 required positional argument`` that the runner
   misclassifies as transient and burns 30+60+120s of retry on.

2. Permanent business-code coverage — privacy / no-resource codes (22115,
   22118, 53016, -400) join 53013 / 88214 in
   ``_PERMANENT_BUSINESS_CODES``.  Without this, every ``followings`` /
   ``followers`` / ``top_videos`` / ``uplikeimg`` call on a uid that opted
   out spends the full retry budget per run.

3. ``NetworkException`` 4xx vs 5xx split — 404 means the route itself is
   gone (e.g. ``dynamics_legacy``) and must surface as
   ``ResourceUnavailableError``; only 5xx is genuinely retryable.
"""

from __future__ import annotations

import inspect

import pytest
from bilibili_api.exceptions import (
    NetworkException,
    ResponseCodeException,
)

from bili_unit.fetching import (
    Http5xxError,
    RequestError,
    ResourceUnavailableError,
)
from bili_unit.fetching._bilibili_adapter import (
    _PERMANENT_BUSINESS_CODES,
    _map_bilibili_errors,
    fetch_user_channels,
)

# -- 1. fetch_user_channels keyword convention --------------------------------

def test_fetch_user_channels_accepts_cred_keyword():
    """The runner's unified call site uses ``cred=...``; the function must
    accept that exact keyword (not ``credential``) or the value silently
    falls into ``**_kw`` and Python raises a missing-arg error."""
    sig = inspect.signature(fetch_user_channels)
    params = sig.parameters
    assert "cred" in params, (
        "fetch_user_channels must accept `cred=` to match fetch_endpoint() "
        "call convention; otherwise every channels fetch fails with "
        "missing-argument and burns the retry budget."
    )
    # `cred` must have a default so the call site can omit it.
    assert params["cred"].default is None


# -- 2. Permanent business-code coverage --------------------------------------

@pytest.mark.parametrize(
    ("code", "label"),
    [
        (53013, "subscribed_bangumi"),  # privacy: list withheld
        (88214, "elec_monthly"),        # charging not enabled
        (22115, "followings"),          # privacy: opt-out
        (22118, "followers"),           # follower list withheld
        (53016, "top_videos"),          # no pinned video
        (-400, "uplikeimg"),            # resource genuinely absent
    ],
)
def test_permanent_business_codes_listed(code: int, label: str):
    """Each terminal user-state code must be in the permanent set so the
    runner skips retries for that endpoint."""
    assert code in _PERMANENT_BUSINESS_CODES, (
        f"code {code} ({label}) is a stable user-state response — retrying "
        f"yields the same answer.  Add to _PERMANENT_BUSINESS_CODES so the "
        f"runner stops burning retry budget on it."
    )


@pytest.mark.parametrize(
    "code",
    [-400, 22115, 22118, 53013, 53016, 88214],
)
@pytest.mark.asyncio
async def test_map_bilibili_errors_promotes_permanent_codes(code: int):
    """A ResponseCodeException with a permanent code must surface as
    ResourceUnavailableError (which the runner classifies as permanent)."""
    with pytest.raises(ResourceUnavailableError):
        async with _map_bilibili_errors("test_endpoint"):
            raise ResponseCodeException(code=code, msg="terminal")


@pytest.mark.asyncio
async def test_map_bilibili_errors_keeps_unknown_codes_retryable():
    """Codes NOT in the permanent set stay as RequestError (retryable) so
    transient business-layer hiccups still get the retry budget."""
    with pytest.raises(RequestError) as exc_info:
        async with _map_bilibili_errors("test_endpoint"):
            raise ResponseCodeException(code=-504, msg="服务调用超时")
    assert "-504" in str(exc_info.value)
    # Must NOT be the permanent class.
    assert not isinstance(exc_info.value, ResourceUnavailableError)


# -- 3. NetworkException status split -----------------------------------------

@pytest.mark.asyncio
async def test_map_bilibili_errors_404_is_permanent():
    """A 404 means the route is gone (e.g. dynamics_legacy is dead) — must
    be permanent so the runner doesn't retry against a dead URL."""
    with pytest.raises(ResourceUnavailableError) as exc_info:
        async with _map_bilibili_errors("dynamics_legacy"):
            raise NetworkException(404, "Not Found")
    assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_map_bilibili_errors_400_is_request_error():
    """Non-404 4xx surfaces as RequestError (retryable but not Http5xxError)
    so the runner doesn't treat client errors as server-side transients."""
    with pytest.raises(RequestError) as exc_info:
        async with _map_bilibili_errors("some_ep"):
            raise NetworkException(400, "Bad Request")
    assert "400" in str(exc_info.value)
    # Must NOT be Http5xxError (which would imply server-side issue).
    assert not isinstance(exc_info.value, Http5xxError)
    # Must NOT have been promoted to permanent.
    assert not isinstance(exc_info.value, ResourceUnavailableError)


@pytest.mark.asyncio
async def test_map_bilibili_errors_5xx_stays_retryable():
    """5xx is the genuine server-transient case — still Http5xxError."""
    with pytest.raises(Http5xxError) as exc_info:
        async with _map_bilibili_errors("some_ep"):
            raise NetworkException(503, "Service Unavailable")
    assert "503" in str(exc_info.value)
