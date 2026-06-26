"""Pagination strategy functions for fetch_endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .._adapter_core import (
    extract_list_items as _extract_list_items,
)
from .._adapter_core import (
    extract_total_count as _extract_total_count,
)
from .._adapter_core import (
    resolve_dot_path as _resolve_dot_path,
)


def _paginate_none(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """No pagination — always last page."""
    return True, None


def _paginate_page(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Generic page pagination: detect list items and total count.

    Supports multiple B站 response shapes:
      videos:       {"list": {"vlist": [...]}, "page": {"count": N}}
      audios:       {"data": [...], "curPage": 1, "pageCount": N, "totalSize": N}
      channel_list: {"items_lists": {"page": {"total": N}, "seasons_list": [...], ...}}
    """
    # --- pagination info ---
    total_count = _extract_total_count(data)

    # Shape 1: standard B站 {"page": {"count": N}} (videos)
    pi = data.get("page")
    if isinstance(pi, dict):
        total_count = total_count or pi.get("count", 0)

    # Shape 2: audio service top-level fields
    if total_count == 0 and "totalSize" in data:
        total_count = data.get("totalSize", 0)

    # Shape 3: channel_list {"items_lists": {"page": {"total": N}}}
    if total_count == 0:
        il_page = _resolve_dot_path(data, "items_lists.page")
        if isinstance(il_page, dict):
            total_count = il_page.get("total", 0)

    # Shape 4: articles {"articles": [...], "pn": N, "ps": N, "count": N}
    if total_count == 0 and "count" in data and isinstance(data["count"], int):
        total_count = data["count"]

    # Shape 5: album {"biz_list": [...], "total_count": N}
    if total_count == 0 and "total_count" in data and isinstance(data["total_count"], int):
        total_count = data["total_count"]

    # --- items ---
    items = _extract_list_items(data, spec.items_path)

    current_pn = request_params.get("pn", 1)
    ps = request_params.get("ps", 30)
    if not items or (total_count > 0 and current_pn * ps >= total_count):
        return True, None
    return False, {**request_params, "pn": current_pn + 1}


def _paginate_cursor(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Cursor pagination: uses has_more + offset."""
    has_more = data.get("has_more", 0) == 1
    if not has_more:
        return True, None
    return False, {**request_params, "offset": data.get("offset", "")}


def _paginate_anchor(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Anchor pagination: response contains an ``anchor`` field pointing to
    the next page's start.  Terminate when anchor is absent or 0."""
    anchor = data.get("anchor", 0)
    if not anchor:
        return True, None
    return False, {**request_params, "anchor": anchor}


def _paginate_legacy_offset(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Legacy offset pagination: uses has_more + next_offset."""
    next_offset = data.get("next_offset", 0)
    has_more = data.get("has_more", 0) == 1
    if not has_more or not next_offset:
        return True, None
    return False, {**request_params, "offset": next_offset}


def _paginate_oid(
    spec: Any,
    data: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """OID pagination: last item's aid/id/oid/param becomes the next offset."""
    items = _extract_list_items(data, spec.items_path)
    ps = request_params.get("ps", 100)
    total_count = _extract_total_count(data)
    if not items or (total_count > 0 and len(items) >= total_count):
        return True, None
    last = items[-1] if isinstance(items[-1], dict) else {}
    next_oid = last.get("aid") or last.get("id") or last.get("oid") or last.get("param")
    if not next_oid or len(items) < ps:
        return True, None
    return False, {**request_params, "oid": next_oid}


_PAGINATION_STRATEGIES: dict[str, Callable] = {
    "none": _paginate_none,
    "page": _paginate_page,
    "cursor": _paginate_cursor,
    "anchor": _paginate_anchor,
    "legacy_offset": _paginate_legacy_offset,
    "oid": _paginate_oid,
}

__all__ = [
    "_PAGINATION_STRATEGIES",
    "_paginate_anchor",
    "_paginate_cursor",
    "_paginate_legacy_offset",
    "_paginate_none",
    "_paginate_oid",
    "_paginate_page",
]
