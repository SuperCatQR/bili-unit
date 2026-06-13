# transform/user_profile — UP 主画像 transform handler.
#
# Consumes an UpProfile typed-object dict from the parsing store.
# The parsing model (UpProfile) has already extracted vip, social,
# stats, and overview from the four source endpoints.  Transform is
# a thin projection: rename keys, drop internal fields, and output
# the canonical structured result.

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "user_profile"
SOURCE_ENDPOINTS: tuple[str, ...] = (
    "user_info", "relation_info", "up_stat", "overview_stat",
)
OPTIONAL_ENDPOINTS: tuple[str, ...] = ("overview_stat",)


class _UserProfileHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    optional_endpoints = OPTIONAL_ENDPOINTS

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Project an UpProfile typed-object dict into a structured result."""
        d = item.item_data

        uid = d.get("mid")
        if uid is None:
            try:
                uid = int(item.item_id)
            except (TypeError, ValueError):
                uid = 0

        result: dict[str, Any] = {
            "uid": uid,
            "name": d.get("name", ""),
            "sex": d.get("sex", ""),
            "sign": d.get("sign", ""),
            "avatar": d.get("avatar", ""),
            "birthday": d.get("birthday", ""),
            "level": d.get("level", 0),
            "vip": dict(d.get("vip", {})),
            "join_time": d.get("jointime", 0),
            "social": dict(d.get("social", {})),
            "stats": dict(d.get("stats", {})),
        }

        # overview is optional — only include when present
        overview = d.get("overview")
        if isinstance(overview, dict) and overview:
            result["overview"] = dict(overview)

        return result


HANDLER: TransformHandler = _UserProfileHandler()
