# opus — OpusPost typed model for the parsing layer.
#
# Source endpoints:
#   opus         (listing, cursor-paginated)
#   opus_detail  (per-opus_id enrichment)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

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
    _model_name: str = "opus"

    id: str = ""
    title: str = ""
    summary: str = ""
    cover: str = ""
    jump_url: str = ""
    stats: OpusStats = field(default_factory=OpusStats)
    ctime: int | None = None
    list_images: list[str] = field(default_factory=list)
    markdown: str = ""
    detail_images: list[dict] = field(default_factory=list)
    cover_local: str = ""
    image_locals: list[str] = field(default_factory=list)

    # -- identity ------------------------------------------------------------

    @property
    def item_id(self) -> str:
        return self.id

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

        # Cover and jump_url
        cover = _str_or_empty(list_item.get("cover"))
        jump_url = _str_or_empty(list_item.get("jump_url"))

        # Title: prefer list_item, fallback to ""
        title = _str_or_empty(list_item.get("title"))

        # Summary: prefer list_item, fallback to modules opus summary text
        summary = _str_or_empty(list_item.get("summary"))
        if not summary:
            summary = _extract_opus_summary_text(modules)

        # List images from modules
        list_images = _extract_opus_pic_urls(modules)

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
                markdown = md
            di = detail.get("images")
            if isinstance(di, list):
                detail_images = [
                    img for img in di if isinstance(img, dict)
                ]

        return cls(
            id=opus_id,
            title=title,
            summary=summary,
            cover=cover,
            jump_url=jump_url,
            stats=stats,
            ctime=ctime,
            list_images=list_images,
            markdown=markdown,
            detail_images=detail_images,
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": self._model_name,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "cover": self.cover,
            "jump_url": self.jump_url,
            "stats": self.stats.to_dict(),
            "ctime": self.ctime,
            "list_images": list(self.list_images),
            "markdown": self.markdown,
            "detail_images": [dict(d) for d in self.detail_images],
            "cover_local": self.cover_local,
            "image_locals": list(self.image_locals),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpusPost:
        stats_d = d.get("stats")
        stats = OpusStats.from_dict(stats_d) if isinstance(stats_d, dict) else OpusStats()

        detail_images_raw = d.get("detail_images", [])
        detail_images = [
            img for img in detail_images_raw if isinstance(img, dict)
        ]

        return cls(
            id=_str_or_empty(d.get("id")),
            title=_str_or_empty(d.get("title")),
            summary=_str_or_empty(d.get("summary")),
            cover=_str_or_empty(d.get("cover")),
            jump_url=_str_or_empty(d.get("jump_url")),
            stats=stats,
            ctime=d.get("ctime"),
            list_images=list(d.get("list_images", []) or []),
            markdown=_str_or_empty(d.get("markdown")),
            detail_images=detail_images,
            cover_local=_str_or_empty(d.get("cover_local")),
            image_locals=list(d.get("image_locals", []) or []),
        )

    # -- image pipeline ------------------------------------------------------

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image download.

        Order: cover first, then detail images (preferred) or list images.
        """
        jobs: list[tuple[str, str]] = []

        # Cover image
        if self.cover:
            jobs.append((self.cover, f"opus/{self.id}_cover.jpg"))

        # Content images: prefer detail images, fallback to list images
        if self.detail_images:
            urls = [
                img.get("url", "")
                for img in self.detail_images
                if isinstance(img, dict)
            ]
        else:
            urls = list(self.list_images)

        for i, url in enumerate(urls):
            if url:
                jobs.append((url, f"opus/{self.id}_{i:02d}.jpg"))

        return jobs

    def apply_image_results(self, results: list[Any]) -> None:
        """Fill cover_local and image_locals from download results.

        The first result (if any) corresponds to the cover; the rest are
        content images.
        """
        ok_results = [r for r in results if r.status in ("ok", "skipped")]

        if self.cover and ok_results:
            self.cover_local = ok_results[0].local_path
            self.image_locals = [r.local_path for r in ok_results[1:]]
        else:
            self.cover_local = ""
            self.image_locals = [r.local_path for r in ok_results]

# ---------------------------------------------------------------------------
# Module-level export expected by models/__init__.py get_parser()
# ---------------------------------------------------------------------------

PARSER = OpusPost
