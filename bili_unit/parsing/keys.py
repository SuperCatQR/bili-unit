# keys — shared KV key helpers for parsing data / task state.
#
# Centralised so that command, query, and tests share the same key-construction
# logic without cross-module private imports.  Mirrors the role of
# bili_unit.fetching.keys / bili_unit.processing.keys.


def _task_key(uid: int) -> str:
    return f"uid:{uid}:task"


def _item_key(uid: int, model: str, item_id: str) -> str:
    """Single-item typed object key.

    model ∈ {"user_profile", "video_detail", "article", "opus", "dynamic"}.
    item_id is a stable string (uid / bvid / cvid / opus_id / dynamic_id).
    """
    return f"uid:{uid}:parse:{model}:{item_id}"


def _item_prefix(uid: int, model: str) -> str:
    """Prefix for listing typed objects for one parsing model."""
    return f"uid:{uid}:parse:{model}:"
