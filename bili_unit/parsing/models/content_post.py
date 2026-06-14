from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text or None


def content_post_item_id(content_key: str) -> str:
    """Return a storage-safe item id for a ContentPost content_key."""
    return content_key.replace(":", "~", 1)


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
    if cross_refs.cvid:
        return f"article:{cross_refs.cvid}"
    if cross_refs.opus_id:
        return f"opus:{cross_refs.opus_id}"
    if cross_refs.bvid:
        return f"video:{cross_refs.bvid}"
    if cross_refs.dynamic_id:
        return f"dynamic:{cross_refs.dynamic_id}"
    return fallback


@dataclass
class ContentPost:
    content_key: str = ""
    kind: str = ""
    title: str = ""
    summary: str = ""
    text: str = ""
    markdown: str = ""
    images: list[str] = field(default_factory=list)
    pub_time: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    @property
    def item_id(self) -> str:
        return content_post_item_id(self.content_key)

    @property
    def is_complete(self) -> bool:
        """True when the post carries enough body to render to a reader.

        Per kind:
        * ``article`` / ``opus`` — body markdown must be populated.
        * ``video`` — must have a title and a bvid cross-ref.
        * ``dynamic*`` — text or images suffice.
        """
        if self.kind in {"article", "opus"}:
            return bool(self.markdown)
        if self.kind == "video":
            return bool(self.title and self.cross_refs.bvid)
        return bool(self.text or self.images)

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": "content_post",
            "_schema_version": 1,
            "content_key": self.content_key,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "text": self.text,
            "markdown": self.markdown,
            "images": list(self.images),
            "pub_time": self.pub_time,
            "stats": dict(self.stats),
            "is_complete": self.is_complete,
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ContentPost:
        cross_refs = CrossRefs.from_dict(value.get("cross_refs") or value.get("_cross_refs"))
        source_refs_raw = value.get("source_refs") or value.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]

        content_key = str(value.get("content_key", "") or "")
        if not content_key:
            content_key = content_key_for_refs(cross_refs)

        return cls(
            content_key=content_key,
            kind=str(value.get("kind", "") or ""),
            title=str(value.get("title", "") or ""),
            summary=str(value.get("summary", "") or ""),
            text=str(value.get("text", "") or ""),
            markdown=str(value.get("markdown", "") or ""),
            images=[str(img) for img in value.get("images", []) or [] if img],
            pub_time=value.get("pub_time"),
            stats=dict(value.get("stats", {}) or {}),
            source_refs=source_refs,
            cross_refs=cross_refs,
        )

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """ContentPost currently normalizes image URLs but does not own downloads."""
        return []

    def apply_image_results(self, results: list[Any]) -> None:
        """No-op image protocol hook for the parsing materializer."""
        return None


PARSER = ContentPost
