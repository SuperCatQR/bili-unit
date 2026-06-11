# keys — shared KV key helpers for fetching data / task / progress stores.
#
# Centralised here so that runner, query, and tests all share the same
# key-construction logic without cross-module private imports.

def _task_key(uid: int) -> str:
    return f"uid:{uid}:task"


def _fetch_key(uid: int, endpoint: str) -> str:
    return f"uid:{uid}:fetch:{endpoint}"


def _progress_key(uid: int, endpoint: str) -> str:
    return f"uid:{uid}:progress:{endpoint}"


def _item_fetch_key(uid: int, endpoint: str, item_id: str) -> str:
    return f"uid:{uid}:fetch:{endpoint}:{item_id}"
