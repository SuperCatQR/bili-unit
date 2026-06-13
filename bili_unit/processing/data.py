# data — file-directory KV store for processing task / proc results / progress.
#
# Mirrors the design of bili_unit.fetching.data.DataStore but maps the
# processing key space:
#
#   uid:{uid}:task                              → {uid}/task.json
#   uid:{uid}:proc:{item_type}:{item_id}        → {uid}/proc/{item_type}/{item_id}.json
#   uid:{uid}:progress:{pipeline}               → {uid}/progress/{pipeline}.json
#   uid:{uid}:progress:{pipeline}:{item_type}   → {uid}/progress/{pipeline}/{item_type}.json
#
# The store itself is a thin wrapper over :class:`bili_unit._storage.JsonKVStore`;
# the processing-specific schema lives in :class:`ProcessingKeyMapper`.

import logging
from pathlib import Path
from typing import Any

from .._storage import KvDataStore, KvSchema, PathShape, SchemaKeyMapper
from . import DataError

logger = logging.getLogger("bili.processing.data")


# Processing key grammar (see module docstring):
#   uid:{uid}:task                            → {uid}/task.json
#   uid:{uid}:proc:{item_type}:{item_id}      → {uid}/proc/{item_type}/{item_id}.json
#   uid:{uid}:progress:{pipeline}             → {uid}/progress/{pipeline}.json
#   uid:{uid}:progress:{pipeline}:{item_type} → {uid}/progress/{pipeline}/{item_type}.json
_PROCESSING_SCHEMA = KvSchema(
    id_prefix="uid",
    sections={
        # task: tail=() → {uid}/task.json (section word is the stem)
        "task": {0: PathShape(dir_indices=(), file_index=0)},
        # proc: tail=(item_type, item_id) → {uid}/proc/{item_type}/{item_id}.json
        "proc": {2: PathShape(dir_indices=(0, 1), file_index=2)},
        # progress: pipeline-only → {uid}/progress/{pipeline}.json
        #           per-item-type → {uid}/progress/{pipeline}/{item_type}.json
        "progress": {
            1: PathShape(dir_indices=(0,), file_index=1),
            2: PathShape(dir_indices=(0, 1), file_index=2),
        },
    },
)


class ProcessingKeyMapper(SchemaKeyMapper):
    """Key ↔ path mapping for the processing key schema."""

    schema = _PROCESSING_SCHEMA


class ProcessingDataStore(KvDataStore):
    """Async file-directory KV store for processing.

    Single asyncio event-loop; writes serialised through the underlying
    :class:`JsonKVStore`'s lock.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, ProcessingKeyMapper(), decode_error_cls=DataError)

    # -- atomic helpers ----------------------------------------------------

    async def update_task_pipeline(
        self,
        task_key: str,
        pipeline: str,
        status: str,
        items: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Atomically update a single pipeline entry inside the task value."""

        def _mutate(tv: dict[str, Any] | None) -> dict[str, Any] | None:
            if tv is None:
                return None
            pipelines = tv.setdefault("pipelines", {})
            entry = pipelines.get(pipeline)
            if entry is None:
                entry = {"status": "PENDING", "items": {}}
                pipelines[pipeline] = entry
            entry["status"] = status
            if items is not None:
                entry["items"] = items
            return tv

        await self._kv.update_in_place(task_key, _mutate)
