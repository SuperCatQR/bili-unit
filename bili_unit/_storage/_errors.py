# _errors — generic per-uid JSON error log with auto-increment IDs.
#
# Stages subclass JsonErrorStore and declare ``extra_fields`` for record keys
# specific to their domain (e.g. ``endpoint`` for fetching, ``pipeline`` /
# ``item_type`` / ``item_id`` for processing).  The base class has nothing
# domain-specific beyond ``id / uid / error_type / message / retryable /
# detail / timestamp``.

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, ClassVar


def normalise_retryable(value: Any) -> bool | None:
    """Coerce a stored ``retryable`` field to tri-state bool.

    Tolerates legacy ``"true"``/``"false"``/``"unknown"`` strings written by
    older versions, plus actual booleans / ``None`` written by current code.
    Anything else (typo, schema drift) → ``None``.  Used by the per-stage
    DTO mappers when reading old JSON records back from disk.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
    return None


class JsonErrorStore:
    """Generic per-uid JSON error log.

    Layout::

        {base}/_counter.json           ← auto-increment ID counter
        {base}/{uid}.json              ← errors for uid (list of records)
        {base}/_null.json              ← errors with uid=None

    Subclasses set ``extra_fields`` to opt extra keyword arguments of
    :meth:`record` into the persisted record.  ``list_records`` returns the
    raw dicts; subclasses map them to stage-specific DTOs.
    """

    extra_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        path: str | Path,
        *,
        decode_error_cls: type[Exception],
    ) -> None:
        self._base = Path(path)
        self._lock = asyncio.Lock()
        self._decode_error_cls = decode_error_cls

    async def open(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass  # no persistent connections to release

    # -- internal helpers --------------------------------------------------

    def _uid_file(self, uid: int | None) -> Path:
        if uid is None:
            return self._base / "_null.json"
        return self._base / f"{uid}.json"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise self._decode_error_cls(f"Corrupted error file {path}: {exc}") from exc

    def _write(self, path: Path, errors: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(errors, ensure_ascii=False), encoding="utf-8")

    def _next_id(self) -> int:
        counter_path = self._base / "_counter.json"
        try:
            data = json.loads(counter_path.read_text(encoding="utf-8"))
            current = data.get("next_id", 1)
        except (FileNotFoundError, json.JSONDecodeError):
            current = 1
        counter_path.write_text(json.dumps({"next_id": current + 1}), encoding="utf-8")
        return current

    # -- public API --------------------------------------------------------

    async def record(
        self,
        error: BaseException,
        uid: int | None = None,
        retryable: bool | None = None,
        detail: dict[str, Any] | None = None,
        **extra: Any,
    ) -> int:
        """Persist an error and return its auto-increment id.

        ``retryable`` is a tri-state bool: ``True`` / ``False`` / ``None``
        (unknown).  Stored verbatim — the JSON encoder writes ``null`` for
        ``None``.  Strings ``"true"``/``"false"``/``"unknown"`` from old
        records on disk are normalised on read by the DTO mappers.

        ``extra`` keys named in ``self.extra_fields`` are pulled into the
        record (defaulting to ``None`` if not supplied).  Other keys in
        ``extra`` are silently ignored — keep the call site explicit.
        """
        error_type = type(error).__name__
        message = str(error)
        detail_raw = json.dumps(detail) if detail else None
        now = int(time.time() * 1000)

        record_extra: dict[str, Any] = {f: extra.get(f) for f in self.extra_fields}

        async with self._lock:
            error_id = self._next_id()
            path = self._uid_file(uid)
            errors = self._read(path)
            errors.append({
                "id": error_id,
                "uid": uid,
                **record_extra,
                "error_type": error_type,
                "message": message,
                "retryable": retryable,
                "detail": detail_raw,
                "timestamp": now,
            })
            self._write(path, errors)
        return error_id

    async def list_records(self, uid: int | None = None) -> list[dict[str, Any]]:
        """Return raw record dicts; subclass DTO mappers handle conversion.

        With a uid filter, returns that uid's records in insertion order.
        Without a filter, scans every file in ``base`` (skipping
        ``_counter.json``) and sorts by ``timestamp`` desc.
        """
        if uid is not None:
            return list(self._read(self._uid_file(uid)))
        all_records: list[dict[str, Any]] = []
        if self._base.is_dir():
            for p in sorted(self._base.glob("*.json")):
                if p.name == "_counter.json":
                    continue
                all_records.extend(self._read(p))
        all_records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return all_records

    async def delete_by_uid(self, uid: int) -> int:
        """Delete all error records for ``uid`` and return the row count."""
        async with self._lock:
            path = self._uid_file(uid)
            records = self._read(path)
            count = len(records)
            if count > 0:
                try:
                    path.unlink()
                except FileNotFoundError:
                    return 0
            return count
