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

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import DataError

logger = logging.getLogger("bili.fetching.data")


class DataStore:
    """Async file-directory key-value store.

    Thread-unsafe by design (single asyncio event-loop).  Writes are serialised
    through an internal async lock.
    """

    def __init__(self, path: str | Path) -> None:
        self._base = Path(path)
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass  # no persistent connections to release

    # -- key → path mapping ------------------------------------------------

    def _key_to_path(self, key: str) -> Path:
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "uid":
            uid, section = parts[1], parts[2]
            if section == "task" and len(parts) == 3:
                return self._base / uid / "task.json"
            if section == "fetch" and len(parts) >= 4:
                ep = parts[3]
                if len(parts) == 4:
                    return self._base / uid / "fetch" / f"{ep}.json"
                item_id = ":".join(parts[4:])
                return self._base / uid / "fetch" / ep / f"{item_id}.json"
            if section == "progress" and len(parts) == 4:
                return self._base / uid / "progress" / f"{parts[3]}.json"
        if len(parts) >= 2 and parts[0] == "rate_limit":
            rl_key = parts[1]
            return self._base / "rate_limit" / f"{rl_key}.json"
        # fallback (should never happen with well-formed keys)
        safe = key.replace(":", "__")
        return self._base / "_misc" / f"{safe}.json"

    def _path_to_key(self, path: Path) -> str:
        rel = path.relative_to(self._base)
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

    # -- internal file I/O (no locking) ------------------------------------

    def _read_file(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise DataError(f"Corrupted value at {path}: {exc}") from exc

    def _write_file(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    # -- basic CRUD ---------------------------------------------------------

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._read_file(self._key_to_path(key))

    async def put(self, key: str, value: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        stored = {**value, "updated_at": now}
        async with self._lock:
            self._write_file(self._key_to_path(key), stored)

    async def delete(self, key: str) -> None:
        async with self._lock:
            path = self._key_to_path(key)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _prefix_to_scan_dir(self, prefix: str) -> Path:
        """Map a key prefix to the directory that contains matching files."""
        parts = prefix.split(":")
        if len(parts) >= 1 and parts[0] == "uid":
            if len(parts) == 1:
                # "uid" → scan entire base (all uid dirs)
                return self._base
            uid = parts[1]
            if len(parts) == 2 or (len(parts) >= 3 and parts[2] == ""):
                # "uid:100" or "uid:100:" → scan the uid directory
                return self._base / uid
            section = parts[2]
            if section == "fetch" and len(parts) == 3:
                return self._base / uid / "fetch"
            if section == "fetch" and len(parts) >= 4 and parts[3] != "":
                return self._base / uid / "fetch" / parts[3]
            if section == "fetch" and len(parts) >= 4 and parts[3] == "":
                return self._base / uid / "fetch"
            if section == "progress":
                return self._base / uid / "progress"
            if section == "task":
                return self._base / uid
        if len(parts) >= 1 and parts[0] == "rate_limit":
            return self._base / "rate_limit"
        # Fallback: try the path as-is; if it's a directory, scan it;
        # otherwise scan parent.
        target = self._key_to_path(prefix)
        return target if target.is_dir() else target.parent

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        scan_dir = self._prefix_to_scan_dir(prefix)

        if not scan_dir.is_dir():
            return []

        results: list[tuple[str, dict[str, Any]]] = []
        for p in sorted(scan_dir.rglob("*.json")):
            key = self._path_to_key(p)
            if key.startswith(prefix):
                value = self._read_file(p)
                if value is not None:
                    results.append((key, value))
        return results

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
        now = int(time.time() * 1000)
        async with self._lock:
            path = self._key_to_path(task_key)
            tv = self._read_file(path)
            if tv is None:
                return

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
            tv["updated_at"] = now

            self._write_file(path, tv)

    # -- transactional write: fetch page + progress in one commit ----------

    async def write_fetch_page_and_progress(
        self,
        fetch_key: str,
        fetch_value: dict[str, Any],
        progress_key: str,
        progress_value: dict[str, Any],
    ) -> None:
        now = int(time.time() * 1000)
        fetch_stored = {**fetch_value, "updated_at": now}
        progress_stored = {**progress_value, "updated_at": now}
        async with self._lock:
            # Write fetch data first, progress last (progress = commit marker).
            # If we crash between the two writes, progress is stale but fetch
            # data is valid.  Next resume re-fetches from old progress and
            # overwrites — idempotent by design.
            self._write_file(self._key_to_path(fetch_key), fetch_stored)
            self._write_file(self._key_to_path(progress_key), progress_stored)
