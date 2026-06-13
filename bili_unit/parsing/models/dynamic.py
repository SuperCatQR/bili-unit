# dynamic — DynamicPost typed model for the parsing layer.
#
# Source endpoint:
#   dynamics  (cursor-paginated)

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .content_post import CrossRefs, SourceRef, content_key_for_refs

logger = logging.getLogger("bili.parsing.models.dynamic")

_OPUS_URL_RE = re.compile(r"/opus/(\d+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str_or_empty(v: Any) -> str:
    """Return a string value, defaulting to "" for None or non-string types."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def _modules_dict(raw: Any) -> dict:
    """Normalise the modules block to a dict.

    The API sometimes returns modules as a dict and sometimes as a list of
    dicts (each with a 'module_type' key).  This helper collapses both shapes
    into a single dict keyed by module_type.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        merged: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            mtype = entry.get("module_type", "")
            if mtype:
                merged[mtype] = entry
            else:
                merged.update(entry)
        return merged
    return {}


def _extract_pub_ts(modules: dict) -> int | None:
    """Extract module_author.pub_ts as an int (or None).

    The field can arrive as a string or int.
    """
    try:
        author = modules.get("module_author", {})
        if not isinstance(author, dict):
            return None
        raw = author.get("pub_ts")
        if raw is None:
            return None
        return int(raw)
    except (ValueError, TypeError):
        return None


def _extract_desc_text(modules: dict) -> str:
    """Extract module_dynamic.desc.text (the textual body of the dynamic)."""
    try:
        md = modules.get("module_dynamic", {})
        if not isinstance(md, dict):
            return ""
        desc = md.get("desc", {})
        if not isinstance(desc, dict):
            return ""
        return _str_or_empty(desc.get("text"))
    except Exception:
        return ""


def _normalise_major(major_raw: Any) -> dict:
    """Normalise a major block into a flat dict with a 'type' key.

    For MAJOR_TYPE_DRAW the images are extracted from draw.items[*].src and
    stored as major["images"] (a flat list of URL strings).

    For MAJOR_TYPE_OPUS the pics are extracted from opus.pics[*].url and
    stored as major["pics"] (a flat list of URL strings).

    For MAJOR_TYPE_ARTICLE the covers are stored as-is.

    For MAJOR_TYPE_ARCHIVE the cover is stored as-is.
    """
    if not isinstance(major_raw, dict):
        return {}

    mtype = _str_or_empty(major_raw.get("type"))
    result: dict[str, Any] = {"type": mtype}

    if mtype == "MAJOR_TYPE_ARCHIVE":
        archive = major_raw.get("archive", {})
        if isinstance(archive, dict):
            result["bvid"] = _str_or_empty(archive.get("bvid"))
            result["aid"] = archive.get("aid")
            result["title"] = _str_or_empty(archive.get("title"))
            result["desc"] = _str_or_empty(archive.get("desc"))
            result["duration_text"] = _str_or_empty(archive.get("duration_text"))
            result["jump_url"] = _str_or_empty(archive.get("jump_url"))
            result["cover"] = _str_or_empty(archive.get("cover"))

    elif mtype == "MAJOR_TYPE_ARTICLE":
        article = major_raw.get("article", {})
        if isinstance(article, dict):
            result["article_id"] = article.get("id")
            result["title"] = _str_or_empty(article.get("title"))
            result["desc"] = _str_or_empty(article.get("desc"))
            result["jump_url"] = _str_or_empty(article.get("jump_url"))
            covers = article.get("covers", [])
            result["covers"] = [
                c for c in covers if isinstance(c, str) and c
            ] if isinstance(covers, list) else []

    elif mtype == "MAJOR_TYPE_DRAW":
        draw = major_raw.get("draw", {})
        if isinstance(draw, dict):
            items = draw.get("items", [])
            if isinstance(items, list):
                result["images"] = [
                    item.get("src", "")
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("src"), str) and item.get("src")
                ]
            else:
                result["images"] = []
        else:
            result["images"] = []

    elif mtype == "MAJOR_TYPE_OPUS":
        opus = major_raw.get("opus", {})
        if isinstance(opus, dict):
            jump_url = _str_or_empty(opus.get("jump_url"))
            match = _OPUS_URL_RE.search(jump_url)
            result["opus_id"] = _str_or_empty(opus.get("opus_id") or opus.get("id") or (match.group(1) if match else ""))
            result["jump_url"] = jump_url
            result["title"] = _str_or_empty(opus.get("title"))
            summary = opus.get("summary", {})
            result["summary_text"] = _str_or_empty(
                summary.get("text") if isinstance(summary, dict) else ""
            )
            pics = opus.get("pics", [])
            result["pics"] = [
                pic.get("url", "")
                for pic in pics
                if isinstance(pic, dict) and isinstance(pic.get("url"), str) and pic.get("url")
            ] if isinstance(pics, list) else []
        else:
            result["opus_id"] = ""
            result["jump_url"] = ""
            result["title"] = ""
            result["summary_text"] = ""
            result["pics"] = []

    else:
        # Unknown or unsupported major type: store raw as-is
        result.update({k: v for k, v in major_raw.items() if k != "type"})

    return result


def _extract_image_urls_from_major(major: dict) -> list[str]:
    """Extract image URLs from a normalised major dict based on its type."""
    mtype = major.get("type", "")

    if mtype == "MAJOR_TYPE_DRAW":
        return list(major.get("images", []))

    elif mtype == "MAJOR_TYPE_ARTICLE":
        return list(major.get("covers", []))

    elif mtype == "MAJOR_TYPE_ARCHIVE":
        cover = major.get("cover", "")
        return [cover] if cover else []

    elif mtype == "MAJOR_TYPE_OPUS":
        return list(major.get("pics", []))

    return []


def _extract_major(modules: dict) -> dict:
    """Extract and normalise the major block from modules.module_dynamic.major."""
    try:
        md = modules.get("module_dynamic", {})
        if not isinstance(md, dict):
            return {}
        major_raw = md.get("major", {})
        return _normalise_major(major_raw)
    except Exception:
        return {}


def _flatten_dynamic(d: dict) -> dict:
    """Flatten a raw dynamic dict into a normalised structure.

    Returns a dict with: id_str, type, text, timestamp, major.
    """
    modules = _modules_dict(d.get("modules"))
    return {
        "id_str": _str_or_empty(d.get("id_str")),
        "type": _str_or_empty(d.get("type")),
        "text": _extract_desc_text(modules),
        "timestamp": _extract_pub_ts(modules),
        "major": _extract_major(modules),
    }


def _cross_refs_from_major(dynamic_id: str, major: dict[str, Any]) -> CrossRefs:
    major_type = _str_or_empty(major.get("type"))
    if major_type == "MAJOR_TYPE_ARTICLE":
        return CrossRefs(
            cvid=_str_or_empty(major.get("article_id")) or None,
            dynamic_id=dynamic_id or None,
        )
    if major_type == "MAJOR_TYPE_OPUS":
        return CrossRefs(
            opus_id=_str_or_empty(major.get("opus_id")) or None,
            dynamic_id=dynamic_id or None,
        )
    if major_type == "MAJOR_TYPE_ARCHIVE":
        return CrossRefs(
            dynamic_id=dynamic_id or None,
            bvid=_str_or_empty(major.get("bvid")) or None,
        )
    return CrossRefs(dynamic_id=dynamic_id or None)


def _target_ref_from_cross_refs(cross_refs: CrossRefs, major_type: str) -> str:
    if cross_refs.cvid:
        return f"article:{cross_refs.cvid}"
    if cross_refs.opus_id:
        return f"opus:{cross_refs.opus_id}"
    if cross_refs.bvid:
        return f"video:{cross_refs.bvid}"
    if major_type == "MAJOR_TYPE_DRAW" and cross_refs.dynamic_id:
        return content_key_for_refs(cross_refs)
    return ""


# ---------------------------------------------------------------------------
# Sub-dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ForwardedDynamic:
    id_str: str = ""
    type: str = ""
    text: str = ""
    timestamp: int | None = None
    major: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id_str": self.id_str,
            "type": self.type,
            "text": self.text,
            "timestamp": self.timestamp,
            "major": dict(self.major),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ForwardedDynamic:
        if not isinstance(d, dict):
            return cls()
        return cls(
            id_str=_str_or_empty(d.get("id_str")),
            type=_str_or_empty(d.get("type")),
            text=_str_or_empty(d.get("text")),
            timestamp=d.get("timestamp"),
            major=dict(d.get("major", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Main dataclass
# ---------------------------------------------------------------------------

@dataclass
class DynamicPost:
    _model_name: str = "dynamic_event"
    _schema_version: int = 1

    id_str: str = ""
    dynamic_id: str = ""
    type: str = ""
    text: str = ""
    timestamp: int | None = None
    pub_time: int | None = None
    major: dict = field(default_factory=dict)
    major_type: str = ""
    target_ref: str = ""
    forwarded_ref: str | None = None
    forwarded: ForwardedDynamic | None = None
    image_urls: list[str] = field(default_factory=list)
    image_locals: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    # -- identity ------------------------------------------------------------

    @property
    def item_id(self) -> str:
        return self.dynamic_id or self.id_str

    # -- raw construction ----------------------------------------------------

    @classmethod
    def from_raw(cls, raw: dict) -> DynamicPost:
        """Build a DynamicPost from a single raw dynamic dict.

        Handles FORWARD dynamics by recursively flattening the `orig` field
        into a ForwardedDynamic.
        """
        flat = _flatten_dynamic(raw)

        # Handle forwarded (orig) dynamic
        forwarded: ForwardedDynamic | None = None
        orig = raw.get("orig")
        if isinstance(orig, dict):
            orig_flat = _flatten_dynamic(orig)
            forwarded = ForwardedDynamic(
                id_str=orig_flat["id_str"],
                type=orig_flat["type"],
                text=orig_flat["text"],
                timestamp=orig_flat["timestamp"],
                major=orig_flat["major"],
            )

        # Collect image URLs from major (and forwarded major)
        image_urls = _extract_image_urls_from_major(flat["major"])
        if forwarded is not None:
            fwd_images = _extract_image_urls_from_major(forwarded.major)
            # Dedup: add forwarded images that aren't already present
            seen = set(image_urls)
            for u in fwd_images:
                if u and u not in seen:
                    seen.add(u)
                    image_urls.append(u)

        major = flat["major"]
        major_type = _str_or_empty(major.get("type"))
        dynamic_id = flat["id_str"]
        cross_refs = _cross_refs_from_major(dynamic_id, major)
        target_ref = _target_ref_from_cross_refs(cross_refs, major_type)
        forwarded_ref = f"dynamic:{forwarded.id_str}" if forwarded is not None and forwarded.id_str else None
        if forwarded is not None:
            fwd_refs = _cross_refs_from_major(forwarded.id_str, forwarded.major)
            if fwd_refs.cvid or fwd_refs.opus_id or fwd_refs.bvid:
                target_ref = _target_ref_from_cross_refs(fwd_refs, _str_or_empty(forwarded.major.get("type")))
            cross_refs = cross_refs.merge_missing(fwd_refs)

        return cls(
            id_str=flat["id_str"],
            dynamic_id=dynamic_id,
            type=flat["type"],
            text=flat["text"],
            timestamp=flat["timestamp"],
            pub_time=flat["timestamp"],
            major=major,
            major_type=major_type,
            target_ref=target_ref,
            forwarded_ref=forwarded_ref,
            forwarded=forwarded,
            image_urls=image_urls,
            source_refs=[SourceRef("dynamics", dynamic_id)] if dynamic_id else [],
            cross_refs=cross_refs,
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": self._model_name,
            "_schema_version": self._schema_version,
            "id_str": self.id_str,
            "dynamic_id": self.dynamic_id or self.id_str,
            "type": self.type,
            "text": self.text,
            "timestamp": self.timestamp,
            "pub_time": self.pub_time if self.pub_time is not None else self.timestamp,
            "major": dict(self.major),
            "major_type": self.major_type,
            "target_ref": self.target_ref,
            "forwarded_ref": self.forwarded_ref,
            "forwarded": self.forwarded.to_dict() if self.forwarded else None,
            "image_urls": list(self.image_urls),
            "image_locals": list(self.image_locals),
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DynamicPost:
        fwd_raw = d.get("forwarded")
        forwarded: ForwardedDynamic | None = None
        if isinstance(fwd_raw, dict):
            forwarded = ForwardedDynamic.from_dict(fwd_raw)
        dynamic_id = _str_or_empty(d.get("dynamic_id") or d.get("id_str"))
        source_refs_raw = d.get("source_refs") or d.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]
        cross_refs = CrossRefs.from_dict(d.get("cross_refs") or d.get("_cross_refs"))
        if not cross_refs.dynamic_id and dynamic_id:
            cross_refs.dynamic_id = dynamic_id

        return cls(
            id_str=dynamic_id,
            dynamic_id=dynamic_id,
            type=_str_or_empty(d.get("type")),
            text=_str_or_empty(d.get("text")),
            timestamp=d.get("timestamp"),
            pub_time=d.get("pub_time") if d.get("pub_time") is not None else d.get("timestamp"),
            major=dict(d.get("major", {}) or {}),
            major_type=_str_or_empty(d.get("major_type") or dict(d.get("major", {}) or {}).get("type")),
            target_ref=_str_or_empty(d.get("target_ref")),
            forwarded_ref=_str_or_empty(d.get("forwarded_ref")) or None,
            forwarded=forwarded,
            image_urls=list(d.get("image_urls", []) or []),
            image_locals=list(d.get("image_locals", []) or []),
            source_refs=source_refs,
            cross_refs=cross_refs,
        )

    # -- image pipeline ------------------------------------------------------

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image download."""
        return [
            (url, f"dynamic/{self.id_str}_{i:02d}.jpg")
            for i, url in enumerate(self.image_urls)
        ]

    def apply_image_results(self, results: list[Any]) -> None:
        """Fill image_locals from download results."""
        self.image_locals = [
            r.local_path for r in results if r.status in ("ok", "skipped")
        ]

# ---------------------------------------------------------------------------
# Module-level export expected by models/__init__.py get_parser()
# ---------------------------------------------------------------------------

PARSER = DynamicPost
