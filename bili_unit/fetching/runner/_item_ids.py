# runner._item_ids — item ID extraction for incremental mode.

import logging
from typing import Any

logger = logging.getLogger("bili.fetching.runner")


def _extract_item_ids(raw_payload: dict[str, Any], path: str | None) -> list[str]:
    """Extract a list of item IDs from raw_payload using a dot-path with [*] expansion.

    Path format: dot-separated segments; ``[*]`` means "expand each element of this list".
    Supports multi-segment paths after [*] (e.g. ``meta.season_id``).
    Examples:
        "list.vlist[*].bvid"                 → raw_payload["list"]["vlist"][i]["bvid"]
        "items[*].id_str"                    → raw_payload["items"][i]["id_str"]
        "items_lists.seasons_list[*].meta.season_id"
                                             → raw_payload["items_lists"]["seasons_list"][i]["meta"]["season_id"]

    Returns an empty list (and logs a warning) on any structural mismatch.
    """
    if path is None:
        return []

    try:
        segments = path.replace("[*]", ".[*]").split(".")
        segments = [s for s in segments if s]  # remove empty segments

        current: Any = raw_payload
        for i, seg in enumerate(segments):
            if seg == "[*]":
                if not isinstance(current, list):
                    logger.warning("item_id_path: expected list at [*], got %s", type(current).__name__)
                    return []
                remaining = segments[i + 1:]
                if not remaining:
                    # [*] is the last segment — return stringified elements
                    return [str(item) for item in current]
                ids: list[str] = []
                for item in current:
                    # Navigate remaining segments into each list element
                    val: Any = item
                    for sub in remaining:
                        if isinstance(val, dict) and sub in val:
                            val = val[sub]
                        else:
                            val = None
                            break
                    if val is not None:
                        ids.append(str(val))
                return ids
            else:
                if isinstance(current, dict) and seg in current:
                    current = current[seg]
                else:
                    logger.warning("item_id_path: key %r not found", seg)
                    return []

        # If we reach here without hitting [*], the path has no list expansion
        if isinstance(current, list):
            return [str(item) for item in current]
        return [str(current)] if current is not None else []

    except Exception as exc:
        logger.warning("item_id_path extraction failed for path %r: %s", path, exc)
        return []


def _extract_item_ids_multi(
    raw_payload: dict[str, Any], paths: list[str],
) -> list[str]:
    """Extract item IDs from raw_payload using multiple dot-paths, aggregating results."""
    ids: list[str] = []
    for path in paths:
        ids.extend(_extract_item_ids(raw_payload, path))
    return ids
