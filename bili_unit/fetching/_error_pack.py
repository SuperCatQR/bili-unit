"""Serializable error pack codec for the F2 worker IPC boundary.

See ``docs/ipc-contract-f2.md`` §7. This module lives in the **main process** and
imports **no** ``bilibili_api``. It defines the wire structure for fetching errors
that cross the worker→main IPC boundary, plus the round-trip codec that lets the
main process reconstruct a fetching exception from a serialized pack and feed it —
unchanged — to ``classify_fetching_exception`` / ``RetryDriver`` /
``store.record_error``.

Key invariant (contract §7.3, "zero behaviour change"): because
``classify_fetching_exception`` only does ``isinstance`` on the fetching exception
classes and ``record_error`` only reads ``type(exc).__name__`` + ``str(exc)``,
reconstructing an exception from ``(type, message)`` is provably sufficient to
reproduce the pre-refactor classification / persistence behaviour. The worker side
runs today's ``map_bilibili_errors`` to turn an SDK exception into a fetching
exception, then serialises it with :func:`error_pack_from_exception`; the main side
rebuilds it with :func:`fetching_exception_from_pack`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

from . import (
    AuthError,
    FetchingError,
    Http5xxError,
    Http412Error,
    InvalidRequestError,
    RequestError,
    ResourceUnavailableError,
)

Classification = Literal["retryable", "permanent", "unavailable"]

#: type-name string -> fetching exception class. The worker serialises the class
#: name; the main process rebuilds the exact type. Keyed by name, so order is
#: irrelevant. Unknown names fall back to :class:`FetchingError` (retryable base).
_TYPE_REGISTRY: dict[str, type[FetchingError]] = {
    "FetchingError": FetchingError,
    "AuthError": AuthError,
    "RequestError": RequestError,
    "InvalidRequestError": InvalidRequestError,
    "Http412Error": Http412Error,
    "Http5xxError": Http5xxError,
    "ResourceUnavailableError": ResourceUnavailableError,
}

_VALID_CLASSIFICATIONS: frozenset[str] = frozenset(get_args(Classification))


def classification_of(exc: BaseException) -> Classification:
    """3-state classification of a fetching exception (contract §7.2 / §7.3).

    This is finer than ``RetryClassification`` (which collapses ``permanent`` and
    ``unavailable`` both to ``PERMANENT``); the 3rd state is carried for worker/main
    parity and observability, but stays consistent with ``classify_fetching_exception``
    — proven by the regression test ``test_fetching_error_pack``.

    NOTE (Step 6, ``resolve_audio_url`` — contract §6.4/§7.2): the audio-download
    failure (``processing.DownloadError``) is **not** a ``FetchingError`` and must map
    to ``permanent``. It must NOT be routed through this default (which would yield
    ``retryable`` and waste the retry budget). The worker side serialises it explicitly
    with ``classification="permanent"`` (see ``bili_worker.errors.download_error_pack``);
    do not rely on the default below for download errors.
    """
    if isinstance(exc, ResourceUnavailableError):
        return "unavailable"
    if isinstance(exc, (AuthError, InvalidRequestError)):
        return "permanent"
    # RequestError (incl. Http412Error / Http5xxError) and bare FetchingError.
    return "retryable"


@dataclass(frozen=True)
class ErrorPack:
    """Serializable fetching-error envelope crossing the IPC boundary (§7.1)."""

    type: str
    classification: Classification
    code: int | None
    message: str
    retryable_hint: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "classification": self.classification,
            "code": self.code,
            "message": self.message,
            "retryable_hint": self.retryable_hint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ErrorPack:
        """Rebuild from a decoded JSON object. Raises ``ValueError`` on a malformed pack.

        Malformed packs are a protocol error (contract §4.2 / §11 exception state);
        the caller surfaces them explicitly rather than silently swallowing.
        """
        try:
            type_ = d["type"]
            classification = d["classification"]
            message = d["message"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed error pack: {d!r}") from exc
        if not isinstance(type_, str) or not isinstance(message, str):
            raise ValueError(f"error pack type/message must be str: {d!r}")
        if classification not in _VALID_CLASSIFICATIONS:
            raise ValueError(f"invalid classification {classification!r}: {d!r}")
        code = d.get("code")
        if code is not None and not isinstance(code, int):
            raise ValueError(f"error pack code must be int|null: {d!r}")
        return cls(
            type=type_,
            classification=classification,
            code=code,
            message=message,
            retryable_hint=bool(d.get("retryable_hint", classification == "retryable")),
        )


def error_pack_from_exception(exc: FetchingError, *, code: int | None = None) -> ErrorPack:
    """Build an :class:`ErrorPack` from a fetching exception (worker side, §7).

    ``code`` is optional diagnostic metadata (HTTP status / business code) the worker
    can extract from the original SDK exception; it does **not** affect classification.
    An unregistered exception type degrades to the ``FetchingError`` base (retryable).
    """
    cls_name = type(exc).__name__
    if cls_name not in _TYPE_REGISTRY:
        cls_name = "FetchingError"
    classification = classification_of(exc)
    return ErrorPack(
        type=cls_name,
        classification=classification,
        code=code,
        message=str(exc),
        retryable_hint=(classification == "retryable"),
    )


def fetching_exception_from_pack(pack: ErrorPack | dict[str, Any]) -> FetchingError:
    """Rebuild the fetching exception from a pack (main side, §7.3).

    The rebuilt instance carries the same concrete type and message as the original,
    which is all ``classify_fetching_exception`` / ``record_error`` read — so the
    downstream classification / persistence behaviour is identical to the pre-refactor
    direct path.
    """
    if isinstance(pack, dict):
        pack = ErrorPack.from_dict(pack)
    exc_cls = _TYPE_REGISTRY.get(pack.type, FetchingError)
    return exc_cls(pack.message)


__all__ = [
    "Classification",
    "ErrorPack",
    "classification_of",
    "error_pack_from_exception",
    "fetching_exception_from_pack",
]
