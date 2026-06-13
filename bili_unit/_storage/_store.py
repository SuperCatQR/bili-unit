# _store — shared CRUD base for the per-stage JSON KV data stores.
#
# Every stage (fetching / parsing / processing) wrapped :class:`JsonKVStore`
# with the same open / close / get / put / delete / list_prefix surface and
# differed only in their atomic task-state helpers.  That shared surface lives
# here; a stage subclasses :class:`KvDataStore`, passes its
# :class:`SchemaKeyMapper`, and adds only its own atomic helpers on top of the
# inherited ``self._kv``.

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._kv import JsonKVStore, KeyMapper, StorageError


class KvDataStore:
    """Async file-directory KV store shared by all stages.

    Holds a :class:`JsonKVStore` and exposes the common CRUD surface.  Atomic,
    stage-specific read-modify-write helpers are added by subclasses against
    the inherited ``self._kv`` (its ``update_in_place`` / ``write_pair_locked``).
    """

    def __init__(
        self,
        path: str | Path,
        mapper: KeyMapper,
        *,
        decode_error_cls: type[Exception] = StorageError,
    ) -> None:
        self._kv = JsonKVStore(path, mapper, decode_error_cls=decode_error_cls)

    async def open(self) -> None:
        await self._kv.open()

    async def close(self) -> None:
        await self._kv.close()

    @property
    def base(self) -> Path:
        return self._kv.base

    # -- basic CRUD --------------------------------------------------------

    async def get(self, key: str) -> dict[str, Any] | None:
        return await self._kv.get(key)

    async def put(self, key: str, value: dict[str, Any]) -> None:
        await self._kv.put(key, value)

    async def delete(self, key: str) -> None:
        await self._kv.delete(key)

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return await self._kv.list_prefix(prefix)
