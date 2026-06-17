# Cross-model identity helpers shared by the typed parsing models.
#
# Every typed parser (article, opus, dynamic, video_detail, video_subtitle,
# up_profile) carries a uniform pair of provenance fields:
#
#   _source_refs : which fetching endpoint(s) produced the row.
#   _cross_refs  : the canonical ids by which other models can join to it
#                  (cvid, opus_id, dynamic_id, bvid).
#
# The shapes live here, not on any one model, because they're *between*
# models — putting them on (say) ContentPost made them load-bearing for a
# materialised view that no longer exists.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text or None


@dataclass(frozen=True)
class SourceRef:
    endpoint: str
    item_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "endpoint": self.endpoint,
            "item_id": self.item_id,
        }

    @classmethod
    def from_dict(cls, value: SourceRef | dict[str, Any]) -> SourceRef:
        if isinstance(value, SourceRef):
            return value
        return cls(
            endpoint=str(value.get("endpoint", "") or ""),
            item_id=str(value.get("item_id", "") or ""),
        )


@dataclass
class CrossRefs:
    cvid: str | None = None
    opus_id: str | None = None
    dynamic_id: str | None = None
    bvid: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "cvid": self.cvid,
            "opus_id": self.opus_id,
            "dynamic_id": self.dynamic_id,
            "bvid": self.bvid,
        }

    @classmethod
    def from_dict(cls, value: CrossRefs | dict[str, Any] | None) -> CrossRefs:
        if isinstance(value, CrossRefs):
            return value
        if not isinstance(value, dict):
            return cls()
        return cls(
            cvid=_str_or_none(value.get("cvid")),
            opus_id=_str_or_none(value.get("opus_id")),
            dynamic_id=_str_or_none(value.get("dynamic_id")),
            bvid=_str_or_none(value.get("bvid")),
        )

    def merge_missing(self, other: CrossRefs) -> CrossRefs:
        return CrossRefs(
            cvid=self.cvid or other.cvid,
            opus_id=self.opus_id or other.opus_id,
            dynamic_id=self.dynamic_id or other.dynamic_id,
            bvid=self.bvid or other.bvid,
        )


def content_key_for_refs(cross_refs: CrossRefs, fallback: str = "") -> str:
    """Pick the canonical content key for a cross-ref bundle.

    Prefers article > opus > video > dynamic; falls back to ``fallback`` if
    none of the four ids is set.  Used by DynamicPost to derive its target
    when a dynamic embeds an article/opus/archive.
    """
    if cross_refs.cvid:
        return f"article:{cross_refs.cvid}"
    if cross_refs.opus_id:
        return f"opus:{cross_refs.opus_id}"
    if cross_refs.bvid:
        return f"video:{cross_refs.bvid}"
    if cross_refs.dynamic_id:
        return f"dynamic:{cross_refs.dynamic_id}"
    return fallback
