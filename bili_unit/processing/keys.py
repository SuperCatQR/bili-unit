# keys — shared KV key helpers for processing data / task / progress / errors.
#
# Centralised so that runner, query, and tests share the same key-construction
# logic without cross-module private imports. Mirrors the role of
# bili_unit.fetching.keys.

def _task_key(uid: int) -> str:
    return f"uid:{uid}:task"


def _proc_key(uid: int, item_type: str, item_id: str) -> str:
    """Single-item processing result key.

    item_type ∈ {"video_metadata", "dynamics", "articles", "audio", ...}.
    item_id is a stable string ID (bvid / id_str / article_id).
    """
    return f"uid:{uid}:proc:{item_type}:{item_id}"


def _progress_key(uid: int, pipeline: str, item_type: str | None = None) -> str:
    """Pipeline progress key.

    Audio progress uses pipeline-only granularity:
        uid:{uid}:progress:audio
    Transform progress is per-item-type:
        uid:{uid}:progress:transform:{item_type}
    """
    if item_type is None:
        return f"uid:{uid}:progress:{pipeline}"
    return f"uid:{uid}:progress:{pipeline}:{item_type}"
