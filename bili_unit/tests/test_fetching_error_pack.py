"""Regression test for the F2 IPC error-pack codec (contract §7).

The decisive guarantee (§7.3, "zero behaviour change"): for every SDK exception the
worker can encounter, running today's ``map_bilibili_errors`` + serialising the result
to an :class:`ErrorPack` + rebuilding the exception on the main side yields the **same**
``classify_fetching_exception`` result as the pre-refactor direct path (SDK exc →
``map_bilibili_errors`` → ``classify_fetching_exception``).

This is the regression baseline the reviewer mandated for Stage 2 implementation review.
"""

from __future__ import annotations

import json

import pytest
from bilibili_api.exceptions import (
    ApiException,
    ArgsException,
    CredentialNoBiliJctException,
    CredentialNoSessdataException,
    NetworkException,
    ResponseCodeException,
)

from bili_unit._retry import RetryClassification
from bili_unit.fetching import (
    AuthError,
    FetchingError,
    Http5xxError,
    Http412Error,
    InvalidRequestError,
    RequestError,
    ResourceUnavailableError,
)
from bili_unit.fetching._adapter_core import map_bilibili_errors
from bili_unit.fetching._error_pack import (
    ErrorPack,
    classification_of,
    error_pack_from_exception,
    fetching_exception_from_pack,
)
from bili_unit.fetching.runner._failure import classify_fetching_exception


async def _map(sdk_exc: BaseException, label: str = "ep") -> FetchingError:
    """Run today's worker-side mapping and return the fetching exception it raises."""
    with pytest.raises(FetchingError) as ei:
        async with map_bilibili_errors(label):
            raise sdk_exc
    return ei.value


# Every SDK exception the worker can hit, paired with the expected fetching type.
# Mirrors the mapping table in docs/ipc-contract-f2.md §7.2.
_SDK_CASES: list[tuple[BaseException, type[FetchingError]]] = [
    (TimeoutError(), Http5xxError),
    (ResponseCodeException(412, "too fast", {}), Http412Error),
    (ResponseCodeException(-400, "请求错误", {}), ResourceUnavailableError),
    (ResponseCodeException(53013, "用户隐私设置未公开", {}), ResourceUnavailableError),
    (ResponseCodeException(88214, "up未开通充电", {}), ResourceUnavailableError),
    (ResponseCodeException(99999, "other", {}), RequestError),
    (NetworkException(404, "not found"), ResourceUnavailableError),
    (NetworkException(403, "forbidden"), RequestError),
    (NetworkException(500, "server"), Http5xxError),
    (NetworkException(0, "conn reset"), Http5xxError),
    (CredentialNoSessdataException(), AuthError),
    (CredentialNoBiliJctException(), AuthError),
    (ArgsException("bad input"), InvalidRequestError),
    (ApiException("generic api error"), RequestError),
    (RuntimeError("totally unexpected"), RequestError),
]


@pytest.mark.parametrize("sdk_exc, expected_type", _SDK_CASES)
async def test_ipc_roundtrip_preserves_classification(
    sdk_exc: BaseException, expected_type: type[FetchingError]
) -> None:
    """SDK exc → map → pack → JSON → rebuild → classify == direct map → classify."""
    # --- pre-refactor direct path (the baseline) ---
    direct_exc = await _map(sdk_exc)
    assert isinstance(direct_exc, expected_type)
    baseline = classify_fetching_exception(direct_exc)

    # --- F2 path: worker maps + packs, frame crosses IPC as JSON, main rebuilds ---
    worker_exc = await _map(sdk_exc)
    pack = error_pack_from_exception(worker_exc)
    frame = json.dumps(pack.to_dict(), ensure_ascii=False)
    assert "\n" not in frame  # contract §4.1: single-line compact frame
    rebuilt = fetching_exception_from_pack(ErrorPack.from_dict(json.loads(frame)))

    # type, message, and final classification all survive the round trip
    assert type(rebuilt) is type(direct_exc)
    assert str(rebuilt) == str(direct_exc)
    assert classify_fetching_exception(rebuilt) == baseline


@pytest.mark.parametrize("sdk_exc, expected_type", _SDK_CASES)
async def test_pack_classification_consistent_with_retry_classification(
    sdk_exc: BaseException, expected_type: type[FetchingError]
) -> None:
    """The 3-state pack classification never contradicts RetryClassification (§7.3)."""
    exc = await _map(sdk_exc)
    pack = error_pack_from_exception(exc)
    retry_cls = classify_fetching_exception(exc)
    if pack.classification == "retryable":
        assert retry_cls is RetryClassification.RETRYABLE
    else:  # "permanent" | "unavailable" both collapse to PERMANENT
        assert retry_cls is RetryClassification.PERMANENT
    # retryable_hint mirrors the 3-state field
    assert pack.retryable_hint is (pack.classification == "retryable")


def test_classification_of_direct() -> None:
    """classification_of maps each fetching type to the contracted 3-state value."""
    assert classification_of(ResourceUnavailableError("x")) == "unavailable"
    assert classification_of(AuthError("x")) == "permanent"
    assert classification_of(InvalidRequestError("x")) == "permanent"
    assert classification_of(Http412Error("x")) == "retryable"
    assert classification_of(Http5xxError("x")) == "retryable"
    assert classification_of(RequestError("x")) == "retryable"
    assert classification_of(FetchingError("x")) == "retryable"


def test_unknown_type_degrades_to_base() -> None:
    """An unregistered type name rebuilds as the FetchingError base (retryable)."""
    pack = ErrorPack(
        type="SomeFutureError",
        classification="retryable",
        code=None,
        message="from a newer worker",
        retryable_hint=True,
    )
    rebuilt = fetching_exception_from_pack(pack)
    assert type(rebuilt) is FetchingError
    assert str(rebuilt) == "from a newer worker"


def test_malformed_pack_raises() -> None:
    """A malformed pack is a protocol error, surfaced explicitly (§4.2/§11)."""
    with pytest.raises(ValueError):
        ErrorPack.from_dict({"type": "AuthError"})  # missing message/classification
    with pytest.raises(ValueError):
        ErrorPack.from_dict({"type": "AuthError", "classification": "bogus", "message": "m"})
    with pytest.raises(ValueError):
        ErrorPack.from_dict({"type": "AuthError", "classification": "permanent", "message": "m", "code": "nope"})


def test_code_is_optional_diagnostic() -> None:
    """``code`` round-trips but does not influence classification."""
    exc = RequestError("ep: code=99999: other")
    pack = error_pack_from_exception(exc, code=99999)
    assert pack.code == 99999
    rebuilt = fetching_exception_from_pack(ErrorPack.from_dict(pack.to_dict()))
    assert classify_fetching_exception(rebuilt) == classify_fetching_exception(exc)
