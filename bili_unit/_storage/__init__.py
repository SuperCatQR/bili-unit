# bili_unit._storage — shared JSON file-directory storage primitives.
#
# Two stage-agnostic building blocks live here:
#
#   * JsonKVStore — file-directory KV store with put/get/delete/list_prefix and
#     atomic helpers (update_in_place, write_pair_locked).  Pluggable via a
#     KeyMapper Protocol so each stage can keep its own key → path schema.
#   * JsonErrorStore — per-uid JSON error log with auto-increment IDs.  Stages
#     subclass it and declare ``extra_fields`` for stage-specific record keys
#     (e.g. ``endpoint`` for fetching, ``pipeline`` / ``item_type`` / ``item_id``
#     for processing).
#
# Both let stages inject their own ``DataError`` subclass for decode failures
# so the public exception surface of each stage stays unchanged.

from ._errors import JsonErrorStore
from ._kv import JsonKVStore, KeyMapper, StorageError

__all__ = [
    "JsonErrorStore",
    "JsonKVStore",
    "KeyMapper",
    "StorageError",
]
