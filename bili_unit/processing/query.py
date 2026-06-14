# query — processing read-only view.
#
# ProcessingQuery exposes the read-only DTO surface for the audio
# pipeline's task / item / error records. Cross-stage aggregate views
# (parsing metadata + audio transcription) live on the unit-level
# :class:`bili_unit.query.BiliQuery`, not here.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import (
    ProcessingErrorDTO,
    ProcessingItemDTO,
    ProcessingItemStatus,
    ProcessingPipelineDTO,
    ProcessingTaskDTO,
    ProcessingTaskStatus,
)
from .keys import _proc_key, _task_key
from .task import ProcessingTaskValue

if TYPE_CHECKING:
    from .data import ProcessingDataStore
    from .error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.query")


class ProcessingQuery:
    """Read-only access to processing results / task state / errors."""

    def __init__(
        self,
        data: ProcessingDataStore,
        error: ProcessingErrorStore,
    ) -> None:
        self._data = data
        self._error = error

    # -- task / pipeline / item -------------------------------------------

    async def get_task(self, uid: int) -> ProcessingTaskDTO | None:
        d = await self._data.get(_task_key(uid))
        if d is None:
            return None
        tv = ProcessingTaskValue.from_dict(d)
        pipelines: dict[str, ProcessingPipelineDTO] = {}
        for name, entry in tv.pipelines.items():
            pipelines[name] = ProcessingPipelineDTO(
                name=name,
                status=entry.status,
                items=dict(entry.items),
            )
        return ProcessingTaskDTO(
            uid=tv.uid,
            status=tv.status,
            pipelines=pipelines,
            created_at=tv.created_at,
            updated_at=tv.updated_at,
        )

    async def list_tasks(self) -> list[dict]:
        rows = await self._data.list_task_rows()
        results: list[dict] = []
        for uid, value in rows:
            try:
                status = ProcessingTaskStatus(value.get("status", "PENDING"))
            except ValueError:
                status = ProcessingTaskStatus.PENDING
            pipeline_count = len(value.get("pipelines", {}))
            results.append({
                "uid": uid,
                "status": status,
                "pipeline_count": pipeline_count,
                "updated_at": value.get("updated_at"),
            })
        return results

    async def get_item(
        self, uid: int, item_type: str, item_id: str,
    ) -> ProcessingItemDTO | None:
        d = await self._data.get(_proc_key(uid, item_type, item_id))
        if d is None:
            return None
        errors = await self._error.list_errors(uid=uid)
        item_errors = [
            e for e in errors
            if e.item_type == item_type and e.item_id == item_id
        ]
        return self._to_item_dto(d, errors=item_errors)

    async def list_items(self, uid: int, item_type: str) -> list[ProcessingItemDTO]:
        prefix = f"uid:{uid}:proc:{item_type}:"
        rows = await self._data.list_prefix(prefix)
        errors = await self._error.list_errors(uid=uid)
        by_item_id: dict[str, list[ProcessingErrorDTO]] = {}
        for e in errors:
            if e.item_type != item_type or e.item_id is None:
                continue
            by_item_id.setdefault(e.item_id, []).append(e)
        return [
            self._to_item_dto(v, errors=by_item_id.get(v.get("item_id", ""), []))
            for _, v in rows
        ]

    # -- errors -----------------------------------------------------------

    async def list_errors(self, uid: int | None = None) -> list[ProcessingErrorDTO]:
        return await self._error.list_errors(uid=uid)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _to_item_dto(
        d: dict, errors: list[ProcessingErrorDTO] | None = None,
    ) -> ProcessingItemDTO:
        try:
            status = ProcessingItemStatus(d.get("status", "PENDING"))
        except ValueError:
            status = ProcessingItemStatus.PENDING
        return ProcessingItemDTO(
            uid=d["uid"],
            pipeline=d["pipeline"],
            item_type=d["item_type"],
            item_id=d["item_id"],
            status=status,
            result=d.get("result"),
            processed_at=d.get("processed_at"),
            errors=errors or [],
        )
