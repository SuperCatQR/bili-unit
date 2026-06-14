# error — file-directory error-state store for processing.
#
# Per docs/design/processing.md §9.2: each uid gets a JSON file with
# error records; errors without a uid go to ``_null.json``.
#
# Each record adds processing-specific fields (pipeline / item_type / item_id)
# on top of the fetching-style fields (id, error_type, message, retryable, ...).
# The store reuses :class:`bili_unit._storage.JsonErrorStore`; this module only
# declares the schema and DTO mapping.

import json
import logging
from typing import Any

from .._storage import JsonErrorStore, normalise_retryable
from . import DataError, ErrorDTO

logger = logging.getLogger("bili.processing.error")


class ProcessingErrorStore(JsonErrorStore):
    """Async file-directory error-state store for processing (separate from data)."""

    extra_fields = ("pipeline", "item_type", "item_id")

    def __init__(self, path) -> None:
        super().__init__(path, decode_error_cls=DataError)

    async def list_errors(self, uid: int | None = None) -> list[ErrorDTO]:
        records = await self.list_records(uid=uid)
        return self._to_dtos(records)

    async def list_by_uid(self, uid: int) -> list[ErrorDTO]:
        return await self.list_errors(uid=uid)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_dtos(records: list[dict[str, Any]]) -> list[ErrorDTO]:
        return [
            ErrorDTO(
                id=r["id"],
                uid=r.get("uid"),
                pipeline=r.get("pipeline"),
                item_type=r.get("item_type"),
                item_id=r.get("item_id"),
                error_type=r["error_type"],
                message=r["message"],
                retryable=normalise_retryable(r.get("retryable")),
                detail=json.loads(r["detail"]) if r.get("detail") else None,
                timestamp=r.get("timestamp"),
            )
            for r in records
        ]
