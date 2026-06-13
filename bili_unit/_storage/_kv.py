# _kv — generic JSON file-directory KV store.
#
# Stage-agnostic core; key → path mapping is delegated to a KeyMapper Protocol
# implementation so each stage (fetching / processing) can keep its own schema.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("bili.storage")


class StorageError(Exception):
    """Base for storage-layer errors raised by JsonKVStore.

    Stages typically inject their own DataError subclass via
    ``decode_error_cls`` so the public exception surface stays unchanged.
    """


class KeyMapper(Protocol):
    """A pluggable mapping between logical keys and on-disk paths.

    Implementations receive ``base`` on every call rather than at construction
    time so a single mapper instance can be shared across stores or tests
    without recomputing the directory tree.
    """

    def to_path(self, base: Path, key: str) -> Path: ...

    def to_key(self, base: Path, path: Path) -> str: ...

    def prefix_to_scan_dir(self, base: Path, prefix: str) -> Path: ...


class JsonKVStore:
    """Async file-directory key-value store.

    Single asyncio event-loop. Writes are serialised through an internal lock.
    Stamps every value with an ``updated_at`` (ms epoch) on ``put()``.
    """

    def __init__(
        self,
        path: str | Path,
        mapper: KeyMapper,
        *,
        decode_error_cls: type[Exception] = StorageError,
    ) -> None:
        self._base = Path(path)
        self._mapper = mapper
        self._lock = asyncio.Lock()
        self._decode_error_cls = decode_error_cls

    async def open(self) -> None:
        await asyncio.to_thread(self._base.mkdir, parents=True, exist_ok=True)

    async def close(self) -> None:
        pass  # no persistent connections to release

    @property
    def base(self) -> Path:
        return self._base

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    # -- raw I/O (no locking) ----------------------------------------------

    def _read_file(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise self._decode_error_cls(f"Corrupted value at {path}: {exc}") from exc

    def _write_file(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    # -- public CRUD --------------------------------------------------------

    async def get(self, key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_file, self._mapper.to_path(self._base, key))

    async def put(self, key: str, value: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        stored = {**value, "updated_at": now}
        async with self._lock:
            await asyncio.to_thread(self._write_file, self._mapper.to_path(self._base, key), stored)

    async def delete(self, key: str) -> None:
        async with self._lock:
            path = self._mapper.to_path(self._base, key)
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(path.unlink)

    def _scan_and_read(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        """Synchronous scan + read for list_prefix."""
        scan_dir = self._mapper.prefix_to_scan_dir(self._base, prefix)
        if not scan_dir.is_dir():
            return []
        results: list[tuple[str, dict[str, Any]]] = []
        for p in sorted(scan_dir.rglob("*.json")):
            key = self._mapper.to_key(self._base, p)
            if key.startswith(prefix):
                value = self._read_file(p)
                if value is not None:
                    results.append((key, value))
        return results

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return await asyncio.to_thread(self._scan_and_read, prefix)

    # -- atomic helpers ----------------------------------------------------

    async def update_in_place(
        self,
        key: str,
        mutator: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        stamp_updated_at: bool = True,
    ) -> None:
        """Read-modify-write atomically under the store lock.

        ``mutator`` receives the current value (or ``None`` if missing) and
        returns the new value (or ``None`` to delete the key).  ``updated_at``
        is stamped on the returned dict unless ``stamp_updated_at=False``.
        Mutator may also mutate the dict in place and return it; the wrapper
        creates a new dict only when stamping ``updated_at``.
        """
        async with self._lock:
            path = self._mapper.to_path(self._base, key)
            current = await asyncio.to_thread(self._read_file, path)
            new_value = mutator(current)
            if new_value is None:
                with contextlib.suppress(FileNotFoundError):
                    await asyncio.to_thread(path.unlink)
                return
            if stamp_updated_at:
                new_value = {**new_value, "updated_at": int(time.time() * 1000)}
            await asyncio.to_thread(self._write_file, path, new_value)

    async def write_pair_locked(
        self,
        key_a: str,
        value_a: dict[str, Any],
        key_b: str,
        value_b: dict[str, Any],
    ) -> None:
        """Write two keys under one lock hold.

        Both values are stamped with the same ``updated_at``.  Caller controls
        write order — the first write commits before the second so it can be
        used as a primary record while the second acts as a commit marker.
        """
        now = int(time.time() * 1000)
        a = {**value_a, "updated_at": now}
        b = {**value_b, "updated_at": now}
        async with self._lock:
            await asyncio.to_thread(self._write_file, self._mapper.to_path(self._base, key_a), a)
            await asyncio.to_thread(self._write_file, self._mapper.to_path(self._base, key_b), b)
