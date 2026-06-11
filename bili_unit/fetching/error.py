# error — file-directory error-state store.
#
# Replaces the previous SQLite-backed implementation.  Each uid gets its own
# JSON file containing a list of error records.  Errors without a uid are
# stored in ``_null.json``.
#
# Directory layout::
#
#   {base}/_counter.json            ← auto-increment ID counter
#   {base}/{uid}.json               ← errors for uid (list of records)
#   {base}/_null.json               ← errors with uid=None

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import DataError, ErrorDTO, FetchingError

logger = logging.getLogger("bili.fetching.error")


class ErrorStore:
    """Async file-directory error-state store (separate from data)."""

    def __init__(self, path: str | Path) -> None:
        self._base = Path(path)
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass  # no persistent connections to release

    # -- internal helpers --------------------------------------------------

    def _uid_file(self, uid: int | None) -> Path:
        if uid is None:
            return self._base / "_null.json"
        return self._base / f"{uid}.json"

    def _read_errors(self, path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise DataError(f"Corrupted error file {path}: {exc}") from exc

    def _write_errors(self, path: Path, errors: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(errors, ensure_ascii=False),
            encoding="utf-8",
        )

    def _next_id(self) -> int:
        counter_path = self._base / "_counter.json"
        try:
            data = json.loads(counter_path.read_text(encoding="utf-8"))
            current = data.get("next_id", 1)
        except (FileNotFoundError, json.JSONDecodeError):
            current = 1
        counter_path.write_text(
            json.dumps({"next_id": current + 1}),
            encoding="utf-8",
        )
        return current

    # -- public API --------------------------------------------------------

    async def record(
        self,
        error: FetchingError,
        uid: int | None = None,
        endpoint: str | None = None,
        retryable: str = "unknown",
        detail: dict[str, Any] | None = None,
    ) -> int:
        """Persist an error and return its id."""
        error_type = type(error).__name__
        message = str(error)
        detail_raw = json.dumps(detail) if detail else None
        now = int(time.time() * 1000)

        async with self._lock:
            error_id = self._next_id()
            path = self._uid_file(uid)
            errors = self._read_errors(path)
            errors.append({
                "id": error_id,
                "uid": uid,
                "endpoint": endpoint,
                "error_type": error_type,
                "message": message,
                "retryable": retryable,
                "detail": detail_raw,
                "timestamp": now,
            })
            self._write_errors(path, errors)
        return error_id

    async def list_errors(self, uid: int | None = None) -> list[ErrorDTO]:
        """Return errors, optionally filtered by uid."""
        if uid is not None:
            records = self._read_errors(self._uid_file(uid))
            return self._to_dtos(records)

        # All errors: scan all files
        all_records: list[dict[str, Any]] = []
        if self._base.is_dir():
            for p in sorted(self._base.glob("*.json")):
                if p.name == "_counter.json":
                    continue
                all_records.extend(self._read_errors(p))
        all_records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return self._to_dtos(all_records)

    async def list_by_uid(self, uid: int) -> list[ErrorDTO]:
        return await self.list_errors(uid=uid)

    async def delete_by_uid(self, uid: int) -> int:
        """Delete all error records for a uid. Returns the number of rows deleted."""
        async with self._lock:
            path = self._uid_file(uid)
            records = self._read_errors(path)
            count = len(records)
            if count > 0:
                try:
                    path.unlink()
                except FileNotFoundError:
                    return 0
            return count

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_dtos(records: list[dict[str, Any]]) -> list[ErrorDTO]:
        return [
            ErrorDTO(
                id=r["id"],
                uid=r.get("uid"),
                endpoint=r.get("endpoint"),
                error_type=r["error_type"],
                message=r["message"],
                retryable=r.get("retryable", "unknown"),
                detail=json.loads(r["detail"]) if r.get("detail") else None,
                timestamp=r.get("timestamp"),
            )
            for r in records
        ]
