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

from .._storage import JsonKVStore
from . import DataError

logger = logging.getLogger("bili.processing.data")


class ProcessingKeyMapper:
    """Key ↔ path mapping for the processing key schema."""

    def to_path(self, base: Path, key: str) -> Path:
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "uid":
            uid, section = parts[1], parts[2]
            if section == "task" and len(parts) == 3:
                return base / uid / "task.json"
            if section == "proc" and len(parts) == 5:
                item_type, item_id = parts[3], parts[4]
                return base / uid / "proc" / item_type / f"{item_id}.json"
            if section == "progress" and len(parts) == 4:
                pipeline = parts[3]
                return base / uid / "progress" / f"{pipeline}.json"
            if section == "progress" and len(parts) == 5:
                pipeline, item_type = parts[3], parts[4]
                return base / uid / "progress" / pipeline / f"{item_type}.json"
        # fallback for malformed keys
        safe = key.replace(":", "__")
        return base / "_misc" / f"{safe}.json"

    def to_key(self, base: Path, path: Path) -> str:
        rel = path.relative_to(base)
        parts = list(rel.parts)
        name = parts[-1]
        if name.endswith(".json"):
            name = name[:-5]

        # {uid}/task.json
        if len(parts) == 2 and name == "task":
            return f"uid:{parts[0]}:task"
        # {uid}/proc/{item_type}/{item_id}.json
        if len(parts) == 4 and parts[1] == "proc":
            return f"uid:{parts[0]}:proc:{parts[2]}:{name}"
        # {uid}/progress/{pipeline}.json
        if len(parts) == 3 and parts[1] == "progress":
            return f"uid:{parts[0]}:progress:{name}"
        # {uid}/progress/{pipeline}/{item_type}.json
        if len(parts) == 4 and parts[1] == "progress":
            return f"uid:{parts[0]}:progress:{parts[2]}:{name}"
        return f"_unknown:{rel}"

    def prefix_to_scan_dir(self, base: Path, prefix: str) -> Path:
        parts = prefix.split(":")
        # treat trailing colon as section-empty (so "uid:1:" → uid dir)
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if parts and parts[0] == "uid":
            if len(parts) == 1:
                return base
            uid = parts[1]
            if len(parts) == 2:
                return base / uid
            section = parts[2] if len(parts) >= 3 else ""
            if section == "proc" and len(parts) == 3:
                return base / uid / "proc"
            if section == "proc" and len(parts) >= 4:
                return base / uid / "proc" / parts[3]
            if section == "progress" and len(parts) == 3:
                return base / uid / "progress"
            if section == "progress" and len(parts) >= 4:
                return base / uid / "progress" / parts[3]
            if section == "task":
                return base / uid
        target = self.to_path(base, prefix)
        return target if target.is_dir() else target.parent


class ProcessingDataStore:
    """Async file-directory KV store for processing.

    Single asyncio event-loop; writes serialised through the underlying
    :class:`JsonKVStore`'s lock.
    """

    def __init__(self, path: str | Path) -> None:
        self._kv = JsonKVStore(
            path,
            ProcessingKeyMapper(),
            decode_error_cls=DataError,
        )

    async def open(self) -> None:
        await self._kv.open()

    async def close(self) -> None:
        await self._kv.close()

    # -- basic CRUD --------------------------------------------------------

    async def get(self, key: str) -> dict[str, Any] | None:
        return await self._kv.get(key)

    async def put(self, key: str, value: dict[str, Any]) -> None:
        await self._kv.put(key, value)

    async def delete(self, key: str) -> None:
        await self._kv.delete(key)

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return await self._kv.list_prefix(prefix)

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
