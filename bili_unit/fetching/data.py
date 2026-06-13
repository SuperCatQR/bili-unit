# data — file-directory KV store for fetching results / task state / progress / rate-limit state.
#
# The public API mirrors the previous SQLite implementation: get / put / delete /
# list_prefix / update_task_endpoint / write_fetch_page_and_progress.
# Keys use the same ``uid:{uid}:…`` format externally; internally they map to
# a directory tree::
#
#   {base}/{uid}/task.json                      ← uid:{uid}:task
#   {base}/{uid}/fetch/{endpoint}.json          ← uid:{uid}:fetch:{endpoint}
#   {base}/{uid}/fetch/{endpoint}/{item}.json   ← uid:{uid}:fetch:{endpoint}:{item}
#   {base}/{uid}/progress/{endpoint}.json       ← uid:{uid}:progress:{endpoint}
#   {base}/rate_limit/global.json               ← rate_limit:global
#   {base}/rate_limit/{key}.json                ← rate_limit:{key}
#
# The store itself is a thin wrapper over :class:`bili_unit._storage.JsonKVStore`;
# the fetching-specific key schema lives in :class:`FetchingKeyMapper`.

import logging
from pathlib import Path
from typing import Any

from .._storage import KvDataStore, KvSchema, PathShape, SchemaKeyMapper
from . import DataError

logger = logging.getLogger("bili.fetching.data")


# Fetching key grammar (cf. SchemaKeyMapper):
#   uid:{uid}:task                      → {uid}/task.json
#   uid:{uid}:fetch:{ep}                → {uid}/fetch/{ep}.json
#   uid:{uid}:fetch:{ep}:{item…}        → {uid}/fetch/{ep}/{item}.json
#   uid:{uid}:progress:{ep}             → {uid}/progress/{ep}.json
#   rate_limit:{key}                    → rate_limit/{key}.json
_FETCHING_SCHEMA = KvSchema(
    id_prefix="uid",
    sections={
        "task": {0: PathShape(dir_indices=(), file_index=0)},
        "fetch": {
            1: PathShape(dir_indices=(0,), file_index=1),
            2: PathShape(dir_indices=(0, 1), file_index=2, overflow_join=True),
        },
        "progress": {1: PathShape(dir_indices=(0,), file_index=1)},
    },
    flat={"rate_limit": PathShape(dir_indices=(0,), file_index=1)},
)


class FetchingKeyMapper(SchemaKeyMapper):
    """Key ↔ path mapping for the fetching key schema."""

    schema = _FETCHING_SCHEMA


class DataStore(KvDataStore):
    """Async file-directory key-value store.

    Thread-unsafe by design (single asyncio event-loop).  Writes are serialised
    through an internal async lock owned by the underlying :class:`JsonKVStore`.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, FetchingKeyMapper(), decode_error_cls=DataError)

    # -- atomic read-modify-write for task state --------------------------

    async def update_task_endpoint(
        self,
        task_key: str,
        ep_name: str,
        status: str,
        retry_count: int = 0,
        last_error_id: int | None = None,
        item_progress: dict | None = None,
    ) -> None:
        """Atomically update a single endpoint entry inside a task value.

        Reads and writes the task value inside a single lock hold so that
        concurrent callers cannot lose updates (lost-update problem).
        """

        def _mutate(tv: dict[str, Any] | None) -> dict[str, Any] | None:
            if tv is None:
                return None
            endpoints = tv.setdefault("endpoints", {})
            entry = endpoints.get(ep_name)
            if entry is None:
                entry = {"status": "PENDING", "retry_count": 0, "last_error_id": None}
                endpoints[ep_name] = entry
            entry["status"] = status
            entry["retry_count"] = retry_count
            if last_error_id is not None:
                entry["last_error_id"] = last_error_id
            if item_progress is not None:
                entry["item_progress"] = item_progress
            return tv

        await self._kv.update_in_place(task_key, _mutate)

    # -- transactional write: fetch page + progress in one commit ----------

    async def write_fetch_page_and_progress(
        self,
        fetch_key: str,
        fetch_value: dict[str, Any],
        progress_key: str,
        progress_value: dict[str, Any],
    ) -> None:
        # Write fetch data first, progress last (progress = commit marker).
        # If we crash between the two writes, progress is stale but fetch
        # data is valid.  Next resume re-fetches from old progress and
        # overwrites — idempotent by design.
        await self._kv.write_pair_locked(
            fetch_key, fetch_value, progress_key, progress_value,
        )
