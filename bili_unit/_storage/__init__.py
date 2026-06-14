# bili_unit._storage — shared JSON file-directory storage primitives.
#
# Stage-agnostic building blocks live here:
#
#   * JsonKVStore — file-directory KV store with put/get/delete/list_prefix and
#     atomic helpers (update_in_place, write_pair_locked).  Pluggable via a
#     KeyMapper Protocol so each stage can keep its own key → path schema.
#   * KvSchema / PathShape / SchemaKeyMapper — a declarative key ↔ path grammar.
#     A stage declares a KvSchema table (its "adapter") and SchemaKeyMapper
#     drives the shared engine against it, so the grammar skeleton lives once.
#   * KvDataStore — the common open/close/CRUD surface every stage's data store
#     shares; stages subclass it and add only their atomic task-state helpers.
#   * JsonErrorStore — per-uid JSON error log with auto-increment IDs.  Stages
#     subclass it and declare ``extra_fields`` for stage-specific record keys
#     (e.g. ``endpoint`` for fetching, ``pipeline`` / ``item_type`` / ``item_id``
#     for processing).
#
# Both let stages inject their own ``DataError`` subclass for decode failures
# so the public exception surface of each stage stays unchanged.

from ._errors import DecodeError, JsonErrorStore, normalise_retryable
from ._kv import JsonKVStore, KeyMapper, StorageError
from ._schema import KvSchema, PathShape, SchemaKeyMapper
from ._store import KvDataStore

__all__ = [
    "DecodeError",
    "JsonErrorStore",
    "JsonKVStore",
    "KeyMapper",
    "KvDataStore",
    "KvSchema",
    "PathShape",
    "SchemaKeyMapper",
    "StorageError",
    "normalise_retryable",
]
