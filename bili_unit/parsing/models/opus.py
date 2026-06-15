# opus — OpusPost typed model for the parsing layer.
#
# Source endpoints:
#   opus         (listing, cursor-paginated)
#   opus_detail  (per-opus_id enrichment)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ._refs import CrossRefs, SourceRef

logger = logging.getLogger("bili.parsing.models.opus")


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


def _url_from_value(v: Any) -> str:
    """Extract a URL string from a B站 image-shaped field.

    The listing payload occasionally returns image-shaped fields as either a
    plain URL string or as ``{"url": ..., "width": ..., "height": ...}``.
    Fall back to ``""`` for unrecognised shapes — never ``str(dict)``.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        u = v.get("url")
        if isinstance(u, str) and u:
            return u
        return ""
    return ""


def _strip_yaml_frontmatter(md: str) -> str:
    """Drop a leading ``---\\n...\\n---`` YAML frontmatter block from ``md``.

    ``bilibili-api``'s ``Opus.markdown()`` prepends a giant frontmatter that
    dumps the raw ``modules`` dict (avatar layers, vip badges, layer_config,
    container_size...).  On real opus posts this frontmatter is ~90% of the
    payload — strip it so the stored markdown is just the body.

    Defensive: if ``md`` doesn't start with ``---\\n`` or no closing ``---``
    is found, return ``md`` unchanged (avoids truncating content that just
    happens to start with ``---``).
    """
    if not md or not md.startswith("---\n"):
        return md
    # Look for the closing ``---`` on its own line (followed by ``\n`` or EOF).
    # Search starts after the opening ``---\n`` so we can't match it as the close.
    rest = md[4:]
    idx = rest.find("\n---\n")
    end: int
    if idx != -1:
        end = 4 + idx + len("\n---\n")
    elif rest.endswith("\n---"):
        end = 4 + len(rest)
    else:
        # No closing fence — treat the leading ``---`` as content, leave alone.
        return md
    body = md[end:]
    # Strip leading blank lines after the closing fence.
    return body.lstrip("\n")


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
                # Merge flat keys when there's no module_type discriminator
                merged.update(entry)
        return merged
    return {}


def _extract_opus_summary_text(modules: dict) -> str:
    """Extract summary text from modules.module_dynamic.major.opus.summary.text."""
    try:
        md = modules.get("module_dynamic", {})
        if not isinstance(md, dict):
            return ""
        major = md.get("major", {})
        if not isinstance(major, dict):
            return ""
        opus = major.get("opus", {})
        if not isinstance(opus, dict):
            return ""
        summary = opus.get("summary", {})
        if not isinstance(summary, dict):
            return ""
        return _str_or_empty(summary.get("text"))
    except Exception:
        return ""


def _extract_opus_pic_urls(modules: dict) -> list[str]:
    """Extract image URLs from modules.module_dynamic.major.opus.pics[*].url."""
    try:
        md = modules.get("module_dynamic", {})
        if not isinstance(md, dict):
            return []
        major = md.get("major", {})
        if not isinstance(major, dict):
            return []
        opus = major.get("opus", {})
        if not isinstance(opus, dict):
            return []
        pics = opus.get("pics", [])
        if not isinstance(pics, list):
            return []
        urls: list[str] = []
        for pic in pics:
            if isinstance(pic, dict):
                u = pic.get("url")
                if isinstance(u, str) and u:
                    urls.append(u)
        return urls
    except Exception:
        return []


_IMAGE_WHITELIST = ("width", "height")


def _merge_images(
    detail_images: list[dict],
    listing_pic_urls: list[str],
) -> list[dict]:
    """Merge detail-endpoint images with listing pic URLs.

    Detail wins (it carries width/height); listing pics fill in only if
    detail didn't supply that URL.  Each entry is whitelisted to
    ``{url, width, height}`` — unknown keys are dropped so the stored
    payload stays small.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for img in detail_images:
        if not isinstance(img, dict):
            continue
        url = img.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        entry: dict[str, Any] = {"url": url}
        for k in _IMAGE_WHITELIST:
            if k in img:
                entry[k] = img[k]
        out.append(entry)
        seen.add(url)
    for url in listing_pic_urls:
        if not isinstance(url, str) or not url or url in seen:
            continue
        out.append({"url": url})
        seen.add(url)
    return out


# ---------------------------------------------------------------------------
# Sub-dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OpusStats:
    view: int = 0
    favorite: int = 0
    like: int = 0
    reply: int = 0
    share: int = 0
    coin: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "view": self.view,
            "favorite": self.favorite,
            "like": self.like,
            "reply": self.reply,
            "share": self.share,
            "coin": self.coin,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpusStats:
        return cls(
            view=int(d.get("view", 0) or 0),
            favorite=int(d.get("favorite", 0) or 0),
            like=int(d.get("like", 0) or 0),
            reply=int(d.get("reply", 0) or 0),
            share=int(d.get("share", 0) or 0),
            coin=int(d.get("coin", 0) or 0),
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> OpusStats:
        if not raw or not isinstance(raw, dict):
            return cls()
        return cls(
            view=int(raw.get("view", 0) or 0),
            favorite=int(raw.get("favorite", 0) or 0),
            like=int(raw.get("like", 0) or 0),
            reply=int(raw.get("reply", 0) or 0),
            share=int(raw.get("share", 0) or 0),
            coin=int(raw.get("coin", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Main dataclass
# ---------------------------------------------------------------------------

@dataclass
class OpusPost:
    _model_name: str = "opus_post"
    _schema_version: int = 2

    id: str = ""
    title: str = ""
    summary: str = ""
    cover: str = ""
    jump_url: str = ""
    stats: OpusStats = field(default_factory=OpusStats)
    ctime: int | None = None
    markdown: str = ""
    # Merged image list. Each entry: {url, width?, height?, local_path?}.
    # Detail-endpoint images win over listing pics; cover stays separate
    # because cover semantics differ.
    images: list[dict] = field(default_factory=list)
    cover_local: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    # -- identity ------------------------------------------------------------

    @property
    def item_id(self) -> str:
        return self.id

    @property
    def is_complete(self) -> bool:
        """True iff the ``opus_detail`` endpoint contributed.

        The opus listing alone lacks the full markdown body; without the
        detail endpoint the post is considered incomplete.
        """
        return any(ref.endpoint == "opus_detail" for ref in self.source_refs)

    # -- raw construction ----------------------------------------------------

    @classmethod
    def from_raw(
        cls,
        list_item: dict,
        detail: dict | None,
    ) -> OpusPost:
        """Build an OpusPost from raw fetching payloads.

        Args:
            list_item: One item dict from the opus listing page.
            detail: Per-opus_id opus_detail raw_payload (or None).
        """
        opus_id = _str_or_empty(list_item.get("opus_id"))

        # Normalise modules
        modules = _modules_dict(list_item.get("modules"))

        # Cover: tolerate both string and {url, width, height} shapes.
        cover = _url_from_value(list_item.get("cover"))
        jump_url = _str_or_empty(list_item.get("jump_url"))

        # Title: prefer list_item, fallback to ""
        title = _str_or_empty(list_item.get("title"))

        # Summary: prefer list_item, fallback to modules opus summary text
        summary = _str_or_empty(list_item.get("summary"))
        if not summary:
            summary = _extract_opus_summary_text(modules)

        # Stats
        stats = OpusStats.from_raw(list_item.get("stats"))

        # ctime: prefer pub_time, then ctime
        raw_ctime = list_item.get("pub_time")
        if raw_ctime is None:
            raw_ctime = list_item.get("ctime")
        ctime: int | None = None
        if raw_ctime is not None:
            try:
                ctime = int(raw_ctime)
            except (ValueError, TypeError):
                ctime = None

        # Detail enrichment
        markdown = ""
        detail_images: list[dict] = []
        if detail and isinstance(detail, dict):
            md = detail.get("markdown")
            if isinstance(md, str):
                markdown = _strip_yaml_frontmatter(md)
            di = detail.get("images")
            if isinstance(di, list):
                detail_images = [img for img in di if isinstance(img, dict)]

        # Build merged images list. Detail wins; fall back to listing pics.
        # Whitelist keys to {url, width, height} — drop anything unknown.
        images = _merge_images(detail_images, _extract_opus_pic_urls(modules))

        source_refs = [SourceRef("opus", opus_id)]
        if detail and isinstance(detail, dict):
            source_refs.append(SourceRef("opus_detail", opus_id))

        return cls(
            id=opus_id,
            title=title,
            summary=summary,
            cover=cover,
            jump_url=jump_url,
            stats=stats,
            ctime=ctime,
            markdown=markdown,
            images=images,
            source_refs=source_refs,
            cross_refs=CrossRefs(opus_id=opus_id),
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": self._model_name,
            "_schema_version": self._schema_version,
            "id": self.id,
            "opus_id": self.id,
            "title": self.title,
            "summary": self.summary,
            "cover": self.cover,
            "jump_url": self.jump_url,
            "stats": self.stats.to_dict(),
            "ctime": self.ctime,
            "pub_time": self.ctime,
            "markdown": self.markdown,
            "images": [dict(img) for img in self.images],
            "cover_local": self.cover_local,
            "is_complete": self.is_complete,
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpusPost:
        stats_d = d.get("stats")
        stats = OpusStats.from_dict(stats_d) if isinstance(stats_d, dict) else OpusStats()

        # Schema migration: v1 stored ``list_images`` (list[str]) +
        # ``detail_images`` (list[dict]) + ``image_locals`` (list[str]).
        # v2 stores a single ``images`` list with embedded ``local_path``.
        images: list[dict]
        if "images" in d and isinstance(d.get("images"), list):
            images = [dict(img) for img in d["images"] if isinstance(img, dict)]
        else:
            detail_images_raw = d.get("detail_images") or []
            list_images_raw = d.get("list_images") or []
            if isinstance(detail_images_raw, list) and any(
                isinstance(x, dict) for x in detail_images_raw
            ):
                images = [
                    dict(img) for img in detail_images_raw if isinstance(img, dict)
                ]
            else:
                images = [
                    {"url": s}
                    for s in list_images_raw
                    if isinstance(s, str) and s
                ]
            # Best-effort positional pairing of legacy image_locals.
            old_locals = d.get("image_locals") or []
            if isinstance(old_locals, list):
                for i, local in enumerate(old_locals):
                    if i < len(images) and isinstance(local, str) and local:
                        images[i]["local_path"] = local

        source_refs_raw = d.get("source_refs") or d.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]
        item_id = _str_or_empty(d.get("id") or d.get("opus_id"))
        cross_refs = CrossRefs.from_dict(d.get("cross_refs") or d.get("_cross_refs"))
        if not cross_refs.opus_id and item_id:
            cross_refs.opus_id = item_id

        return cls(
            id=item_id,
            title=_str_or_empty(d.get("title")),
            summary=_str_or_empty(d.get("summary")),
            cover=_url_from_value(d.get("cover")),
            jump_url=_str_or_empty(d.get("jump_url")),
            stats=stats,
            ctime=d.get("ctime") if d.get("ctime") is not None else d.get("pub_time"),
            markdown=_str_or_empty(d.get("markdown")),
            images=images,
            cover_local=_str_or_empty(d.get("cover_local")),
            source_refs=source_refs,
            cross_refs=cross_refs,
        )

    # -- image pipeline ------------------------------------------------------

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image download.

        Order: cover (if any) first, then ``self.images`` in stored order.
        """
        jobs: list[tuple[str, str]] = []

        if self.cover:
            jobs.append((self.cover, f"opus/{self.id}_cover.jpg"))

        for i, img in enumerate(self.images):
            url = img.get("url", "") if isinstance(img, dict) else ""
            if url:
                jobs.append((url, f"opus/{self.id}_{i:02d}.jpg"))

        return jobs

    def apply_image_results(self, results: list[Any]) -> None:
        """Attach local paths to ``cover_local`` and each ``images[i]``.

        Pairs results to entries by URL identity rather than positional
        index — this keeps cover_local correct when the cover download
        fails but content downloads succeed.
        """
        ok = {
            r.url: r.local_path
            for r in results
            if r.status in ("ok", "skipped")
        }
        self.cover_local = ok.get(self.cover, "") if self.cover else ""
        for img in self.images:
            if not isinstance(img, dict):
                continue
            u = img.get("url", "")
            img["local_path"] = ok.get(u, "")

# ---------------------------------------------------------------------------
# Module-level export expected by models/__init__.py get_parser()
# ---------------------------------------------------------------------------

PARSER = OpusPost
