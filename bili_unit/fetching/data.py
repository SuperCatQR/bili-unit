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

from .._storage import JsonKVStore
from . import DataError

logger = logging.getLogger("bili.fetching.data")


class FetchingKeyMapper:
    """Key ↔ path mapping for the fetching key schema."""

    def to_path(self, base: Path, key: str) -> Path:
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "uid":
            uid, section = parts[1], parts[2]
            if section == "task" and len(parts) == 3:
                return base / uid / "task.json"
            if section == "fetch" and len(parts) >= 4:
                ep = parts[3]
                if len(parts) == 4:
                    return base / uid / "fetch" / f"{ep}.json"
                item_id = ":".join(parts[4:])
                return base / uid / "fetch" / ep / f"{item_id}.json"
            if section == "progress" and len(parts) == 4:
                return base / uid / "progress" / f"{parts[3]}.json"
        if len(parts) >= 2 and parts[0] == "rate_limit":
            rl_key = parts[1]
            return base / "rate_limit" / f"{rl_key}.json"
        # fallback (should never happen with well-formed keys)
        safe = key.replace(":", "__")
        return base / "_misc" / f"{safe}.json"

    def to_key(self, base: Path, path: Path) -> str:
        rel = path.relative_to(base)
        parts = list(rel.parts)
        name = parts[-1]
        if name.endswith(".json"):
            name = name[:-5]

        # {uid}/task.json → uid:{uid}:task
        if len(parts) == 2 and name == "task":
            return f"uid:{parts[0]}:task"
        # {uid}/fetch/{ep}.json → uid:{uid}:fetch:{ep}
        if len(parts) == 3 and parts[1] == "fetch":
            return f"uid:{parts[0]}:fetch:{name}"
        # {uid}/fetch/{ep}/{item}.json → uid:{uid}:fetch:{ep}:{item}
        if len(parts) == 4 and parts[1] == "fetch":
            return f"uid:{parts[0]}:fetch:{parts[2]}:{name}"
        # {uid}/progress/{ep}.json → uid:{uid}:progress:{ep}
        if len(parts) == 3 and parts[1] == "progress":
            return f"uid:{parts[0]}:progress:{name}"
        # rate_limit/{key}.json → rate_limit:{key}
        if len(parts) == 2 and parts[0] == "rate_limit":
            return f"rate_limit:{name}"
        return f"_unknown:{rel}"

    def prefix_to_scan_dir(self, base: Path, prefix: str) -> Path:
        """Map a key prefix to the directory that contains matching files."""
        parts = prefix.split(":")
        if len(parts) >= 1 and parts[0] == "uid":
            if len(parts) == 1:
                # "uid" → scan entire base (all uid dirs)
                return base
            uid = parts[1]
            if len(parts) == 2 or (len(parts) >= 3 and parts[2] == ""):
                # "uid:100" or "uid:100:" → scan the uid directory
                return base / uid
            section = parts[2]
            if section == "fetch" and len(parts) == 3:
                return base / uid / "fetch"
            if section == "fetch" and len(parts) >= 4 and parts[3] != "":
                return base / uid / "fetch" / parts[3]
            if section == "fetch" and len(parts) >= 4 and parts[3] == "":
                return base / uid / "fetch"
            if section == "progress":
                return base / uid / "progress"
            if section == "task":
                return base / uid
        if len(parts) >= 1 and parts[0] == "rate_limit":
            return base / "rate_limit"
        # Fallback: try the path as-is; if it's a directory, scan it;
        # otherwise scan parent.
        target = self.to_path(base, prefix)
        return target if target.is_dir() else target.parent


class DataStore:
    """Async file-directory key-value store.

    Thread-unsafe by design (single asyncio event-loop).  Writes are serialised
    through an internal async lock owned by the underlying :class:`JsonKVStore`.
    """

    def __init__(self, path: str | Path) -> None:
        self._kv = JsonKVStore(
            path,
            FetchingKeyMapper(),
            decode_error_cls=DataError,
        )

    async def open(self) -> None:
        await self._kv.open()

    async def close(self) -> None:
        await self._kv.close()

    # -- basic CRUD ---------------------------------------------------------

    async def get(self, key: str) -> dict[str, Any] | None:
        return await self._kv.get(key)

    async def put(self, key: str, value: dict[str, Any]) -> None:
        await self._kv.put(key, value)

    async def delete(self, key: str) -> None:
        await self._kv.delete(key)

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return await self._kv.list_prefix(prefix)

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
