# data — file-directory KV store for parsing typed objects / task state.
#
# Mirrors the design of bili_unit.fetching.data.DataStore and
# bili_unit.processing.data.ProcessingDataStore but maps the parsing key space:
#
#   uid:{uid}:task                            → {uid}/task.json
#   uid:{uid}:parse:{model}:{item_id}         → {uid}/{model}/{item_id}.json
#
# user_profile is a special case where item_id == str(uid), so it ends up at
# {uid}/user_profile/{uid}.json — a single file per uid.

import logging
from pathlib import Path
from typing import Any

from .._storage import KvDataStore, KvSchema, PathShape, SchemaKeyMapper
from . import DataError

logger = logging.getLogger("bili.parsing.data")


# Parsing key grammar (cf. module docstring):
#   uid:{uid}:task                      → {uid}/task.json
#   uid:{uid}:parse:{model}:{item_id}   → {uid}/{model}/{item_id}.json
# The ``parse`` section drops its own word from the path — the model name is
# the first directory segment, the item id the filename stem.
PARSING_SCHEMA = KvSchema(
    id_prefix="uid",
    sections={
        "task": {0: PathShape(dir_indices=(), file_index=0)},
        "parse": {2: PathShape(dir_indices=(1,), file_index=2)},
    },
)


class ParsingKeyMapper(SchemaKeyMapper):
    """Key ↔ path mapping for the parsing key schema."""

    schema = PARSING_SCHEMA


class ParsingDataStore(KvDataStore):
    """Async file-directory KV store for parsing.

    Single asyncio event-loop; writes serialised through the underlying
    JsonKVStore's lock.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, ParsingKeyMapper(), decode_error_cls=DataError)

    # -- atomic helpers ----------------------------------------------------

    async def update_task_model_status(
        self,
        task_key: str,
        model: str,
        status: str,
        count: int = 0,
    ) -> None:
        """Atomically update a single model entry inside the task value."""

        def _mutate(tv: dict[str, Any] | None) -> dict[str, Any] | None:
            if tv is None:
                return None
            models = tv.setdefault("models", {})
            entry = models.get(model)
            if entry is None:
                entry = {"status": "PENDING", "count": 0}
                models[model] = entry
            entry["status"] = status
            entry["count"] = count
            return tv

        await self._kv.update_in_place(task_key, _mutate)

    async def update_task_images(
        self,
        task_key: str,
        images_summary: dict[str, Any],
    ) -> None:
        """Atomically update the images block in the task value."""

        def _mutate(tv: dict[str, Any] | None) -> dict[str, Any] | None:
            if tv is None:
                return None
            tv["images"] = images_summary
            return tv

        await self._kv.update_in_place(task_key, _mutate)
