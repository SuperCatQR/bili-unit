"""Main-side NDJSON codec mirror (arm's-length — does NOT import bili_worker).

This is the main-process half of the IPC protocol defined in
docs/ipc-contract-f2.md §4. It mirrors bili_worker.protocol without
sharing code, keeping the arm's-length boundary intact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["ok", "error"]


class ProtocolError(ValueError):
    """Malformed frame or non-conforming response (contract §4.2)."""


def encode_frame(obj: dict[str, Any]) -> str:
    """Serialize one envelope to a single-line NDJSON frame (trailing ``\\n``)."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    assert "\n" not in line, "compact JSON frame must be single-line"
    return line + "\n"


def decode_frame(line: str) -> dict[str, Any]:
    """Parse one NDJSON frame to a dict. Raises ProtocolError if malformed."""
    line = line.strip()
    if not line:
        raise ProtocolError("empty frame")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"frame must be a JSON object, got {type(obj).__name__}")
    return obj


@dataclass(frozen=True)
class Request:
    """Request envelope: ``{"id": int, "op": str, "params": object}``."""

    id: int
    op: str
    params: dict[str, Any]

    def to_frame(self) -> str:
        return encode_frame({"id": self.id, "op": self.op, "params": self.params})


@dataclass(frozen=True)
class Response:
    """Decoded response: exactly one of ``data`` / ``error`` per ``status``."""

    id: int
    status: Status
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> Response:
        try:
            resp_id = obj["id"]
            status = obj["status"]
        except (KeyError, TypeError) as exc:
            raise ProtocolError(f"response missing id/status: {obj!r}") from exc
        if not isinstance(resp_id, int) or isinstance(resp_id, bool):
            raise ProtocolError(f"response id must be int: {obj!r}")
        if status == "ok":
            data = obj.get("data")
            if not isinstance(data, dict):
                raise ProtocolError(f"ok response must carry data object: {obj!r}")
            return cls(id=resp_id, status="ok", data=data)
        if status == "error":
            error = obj.get("error")
            if not isinstance(error, dict):
                raise ProtocolError(f"error response must carry error object: {obj!r}")
            return cls(id=resp_id, status="error", error=error)
        raise ProtocolError(f"response status must be ok|error: {obj!r}")


ok_response = bili_worker_protocol_ok = lambda req_id, data: {"id": req_id, "status": "ok", "data": data}  # noqa: E731
"""Build a success response envelope. Defined inline to avoid code drift from the worker."""


__all__ = [
    "ProtocolError",
    "Request",
    "Response",
    "Status",
    "decode_frame",
    "encode_frame",
]
