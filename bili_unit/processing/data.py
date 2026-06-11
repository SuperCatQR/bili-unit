# data — file-directory KV store for processing task / proc results / progress.
#
# Mirrors the design of bili_unit.fetching.data.DataStore but maps the
# processing key space:
#
#   uid:{uid}:task                              → {uid}/task.json
#   uid:{uid}:proc:{item_type}:{item_id}        → {uid}/proc/{item_type}/{item_id}.json
#   uid:{uid}:progress:{pipeline}               → {uid}/progress/{pipeline}.json
#   uid:{uid}:progress:{pipeline}:{item_type}   → {uid}/progress/{pipeline}/{item_type}.json

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import DataError

logger = logging.getLogger("bili.processing.data")


class ProcessingDataStore:
    """Async file-directory KV store for processing.

    Single asyncio event-loop; writes serialised through an internal lock.
    """

    def __init__(self, path: str | Path) -> None:
        self._base = Path(path)
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass  # no persistent connections to release

    # -- key → path --------------------------------------------------------

    def _key_to_path(self, key: str) -> Path:
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "uid":
            uid, section = parts[1], parts[2]
            if section == "task" and len(parts) == 3:
                return self._base / uid / "task.json"
            if section == "proc" and len(parts) == 5:
                item_type, item_id = parts[3], parts[4]
                return self._base / uid / "proc" / item_type / f"{item_id}.json"
            if section == "progress" and len(parts) == 4:
                pipeline = parts[3]
                return self._base / uid / "progress" / f"{pipeline}.json"
            if section == "progress" and len(parts) == 5:
                pipeline, item_type = parts[3], parts[4]
                return self._base / uid / "progress" / pipeline / f"{item_type}.json"
        # fallback for malformed keys
        safe = key.replace(":", "__")
        return self._base / "_misc" / f"{safe}.json"

    def _path_to_key(self, path: Path) -> str:
        rel = path.relative_to(self._base)
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

    # -- internal file I/O -------------------------------------------------

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

    # -- basic CRUD --------------------------------------------------------

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
        parts = prefix.split(":")
        # treat trailing colon as section-empty (so "uid:1:" → uid dir)
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if parts and parts[0] == "uid":
            if len(parts) == 1:
                return self._base
            uid = parts[1]
            if len(parts) == 2:
                return self._base / uid
            section = parts[2] if len(parts) >= 3 else ""
            if section == "proc" and len(parts) == 3:
                return self._base / uid / "proc"
            if section == "proc" and len(parts) >= 4:
                return self._base / uid / "proc" / parts[3]
            if section == "progress" and len(parts) == 3:
                return self._base / uid / "progress"
            if section == "progress" and len(parts) >= 4:
                return self._base / uid / "progress" / parts[3]
            if section == "task":
                return self._base / uid
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

    # -- atomic helpers ----------------------------------------------------

    async def update_task_pipeline(
        self,
        task_key: str,
        pipeline: str,
        status: str,
        items: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Atomically update a single pipeline entry inside the task value."""
        now = int(time.time() * 1000)
        async with self._lock:
            path = self._key_to_path(task_key)
            tv = self._read_file(path)
            if tv is None:
                return
            pipelines = tv.setdefault("pipelines", {})
            entry = pipelines.get(pipeline)
            if entry is None:
                entry = {"status": "PENDING", "items": {}}
                pipelines[pipeline] = entry
            entry["status"] = status
            if items is not None:
                entry["items"] = items
            tv["updated_at"] = now
            self._write_file(path, tv)
