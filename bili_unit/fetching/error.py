# error — file-directory error-state store.
#
# Each uid gets its own JSON file containing a list of error records.  Errors
# without a uid are stored in ``_null.json``.
#
# Directory layout::
#
#   {base}/_counter.json            ← auto-increment ID counter
#   {base}/{uid}.json               ← errors for uid (list of records)
#   {base}/_null.json               ← errors with uid=None
#
# The store reuses :class:`bili_unit._storage.JsonErrorStore`; this module only
# declares the fetching-specific record schema (``endpoint``) and the DTO
# mapping.

import json
import logging
from typing import Any

from .._storage import JsonErrorStore, normalise_retryable
from . import DataError, FetchingErrorDTO

logger = logging.getLogger("bili.fetching.error")


class ErrorStore(JsonErrorStore):
    """Async file-directory error-state store for fetching."""

    extra_fields = ("endpoint",)

    def __init__(self, path) -> None:
        super().__init__(path, decode_error_cls=DataError)

    async def list_errors(self, uid: int | None = None) -> list[FetchingErrorDTO]:
        """Return errors, optionally filtered by uid."""
        records = await self.list_records(uid=uid)
        return self._to_dtos(records)

    async def list_by_uid(self, uid: int) -> list[FetchingErrorDTO]:
        return await self.list_errors(uid=uid)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_dtos(records: list[dict[str, Any]]) -> list[FetchingErrorDTO]:
        return [
            FetchingErrorDTO(
                id=r["id"],
                uid=r.get("uid"),
                endpoint=r.get("endpoint"),
                error_type=r["error_type"],
                message=r["message"],
                retryable=normalise_retryable(r.get("retryable")),
                detail=json.loads(r["detail"]) if r.get("detail") else None,
                timestamp=r.get("timestamp"),
            )
            for r in records
        ]
