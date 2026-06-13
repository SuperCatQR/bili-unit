# transform/dynamics — 动态内容 transform handler.
#
# Consumes a DynamicPost typed-object dict from the parsing store.
# The parsing model has already flattened modules, extracted text,
# timestamp, major, and forwarded dynamics.  Transform projects the
# typed-object dict into the canonical structured result.

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "dynamics"
SOURCE_ENDPOINTS: tuple[str, ...] = ("dynamics",)


class _DynamicsHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Project a DynamicPost typed-object dict into a structured result."""
        d = item.item_data

        forwarded_raw = d.get("forwarded")
        forwarded: dict[str, Any] | None = None
        if isinstance(forwarded_raw, dict):
            forwarded = {
                "id_str": forwarded_raw.get("id_str", ""),
                "type": forwarded_raw.get("type", ""),
                "text": forwarded_raw.get("text", ""),
                "timestamp": forwarded_raw.get("timestamp"),
                "major": dict(forwarded_raw.get("major", {})),
            }

        return {
            "id_str": d.get("id_str", "") or item.item_id,
            "type": d.get("type", ""),
            "text": d.get("text", ""),
            "timestamp": d.get("timestamp"),
            "major": dict(d.get("major", {})),
            "forwarded": forwarded,
        }


HANDLER: TransformHandler = _DynamicsHandler()
