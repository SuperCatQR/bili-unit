# video_subtitle -- typed model for the video_subtitle parsing slot.
#
# Source endpoint: video_subtitle (item-level fanout, per-bvid).
# Each raw payload carries the page list and a per-page subtitle struct
# whose ``content`` array contains one entry per language with an inline
# ``body`` of timed segments (when the subtitle URL fetch succeeded).
#
# See docs/structure/fetching-contract.md §4.7 for the upstream shape.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ._refs import CrossRefs, SourceRef

logger = logging.getLogger("bili.parsing.models.video_subtitle")


# Language priority — by ``lan`` *prefix* match. The first prefix that
# resolves to a non-empty body for a given page wins.
_LANG_PRIORITY: tuple[str, ...] = ("zh-CN", "zh-Hans", "zh-HK", "ai-zh", "en")


# ---------------------------------------------------------------------------
# Nested dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SubtitleSegment:
    """One timed subtitle segment within a page."""

    start: float = 0.0  # ``body[*].from`` — seconds, relative to page start
    end: float = 0.0    # ``body[*].to``
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubtitleSegment:
        return cls(
            start=float(d.get("start", 0.0) or 0.0),
            end=float(d.get("end", 0.0) or 0.0),
            content=str(d.get("content", "") or ""),
        )


@dataclass
class SubtitlePage:
    """The selected-language subtitle for one video page."""

    page_index: int = 0
    cid: int = 0
    lan: str = ""        # selected default language; "" means no body found
    lan_doc: str = ""
    segments: list[SubtitleSegment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "cid": self.cid,
            "lan": self.lan,
            "lan_doc": self.lan_doc,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubtitlePage:
        seg_list = d.get("segments", []) or []
        segments = [
            SubtitleSegment.from_dict(s) for s in seg_list if isinstance(s, dict)
        ]
        return cls(
            page_index=int(d.get("page_index", 0) or 0),
            cid=int(d.get("cid", 0) or 0),
            lan=str(d.get("lan", "") or ""),
            lan_doc=str(d.get("lan_doc", "") or ""),
            segments=segments,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_segments(body: Any) -> list[SubtitleSegment]:
    """Convert a raw ``body`` list to ``SubtitleSegment`` objects."""
    if not isinstance(body, list):
        return []
    out: list[SubtitleSegment] = []
    for raw in body:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("from", 0.0) or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(raw.get("to", 0.0) or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        content = str(raw.get("content", "") or "")
        out.append(SubtitleSegment(start=start, end=end, content=content))
    return out


def _select_language(content: list[Any]) -> tuple[str, str, list[SubtitleSegment]]:
    """Pick the best language entry from a page's ``content`` array.

    Priority (by ``lan`` prefix): zh-CN > zh-Hans > zh-HK > ai-zh > en >
    first non-empty body.

    Returns ``(lan, lan_doc, segments)`` — empty strings + empty list when
    nothing is usable (every entry has ``_fetch_error`` or empty body).
    """
    # Build candidate map: lan -> (lan_doc, segments). Skip _fetch_error and
    # empty bodies — we only want languages with usable content.
    usable: list[tuple[str, str, list[SubtitleSegment]]] = []
    for entry in content:
        if not isinstance(entry, dict):
            continue
        if entry.get("_fetch_error"):
            continue
        body = entry.get("body")
        segments = _coerce_segments(body)
        if not segments:
            continue
        lan = str(entry.get("lan", "") or "")
        lan_doc = str(entry.get("lan_doc", "") or "")
        usable.append((lan, lan_doc, segments))

    if not usable:
        return "", "", []

    # Priority lookup by prefix.
    for prefix in _LANG_PRIORITY:
        for lan, lan_doc, segments in usable:
            if lan.startswith(prefix):
                return lan, lan_doc, segments

    # Fallback: first usable.
    return usable[0]


# ---------------------------------------------------------------------------
# Main model
# ---------------------------------------------------------------------------

@dataclass
class VideoSubtitle:
    """Typed representation of subtitle text for a single Bilibili video."""

    _model_name: str = "video_subtitle"
    _schema_version: int = 1

    bvid: str = ""
    pages: list[SubtitlePage] = field(default_factory=list)
    available_languages: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    # -----------------------------------------------------------------------
    # Common interface
    # -----------------------------------------------------------------------

    @property
    def item_id(self) -> str:
        return self.bvid

    @property
    def is_complete(self) -> bool:
        """True iff every page resolved at least one language with a body."""
        return bool(self.pages) and all(p.lan for p in self.pages)

    @classmethod
    def from_raw(cls, bvid: str, raw: dict) -> VideoSubtitle:
        """Build from the fetching ``video_subtitle`` raw_payload.

        ``raw`` shape (see fetching-contract §4.7):
            {"pages": [...],
             "subtitle": [
                 {"page_index": 0, "cid": ..., "part": "...",
                  "result": {...}, "content": [{"lan", "lan_doc", "body"|_fetch_error}, ...]},
                 ...
             ]}

        Pages whose ``content`` has no usable body for any language are
        skipped entirely (``is_complete`` will then be False).
        """
        pages_out: list[SubtitlePage] = []
        seen_langs: list[str] = []  # preserve discovery order, dedup

        sub_list = raw.get("subtitle", []) if isinstance(raw, dict) else []
        if not isinstance(sub_list, list):
            sub_list = []

        for entry in sub_list:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content", [])
            if not isinstance(content, list):
                content = []

            # Track every language with a usable body for ``available_languages``.
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("_fetch_error"):
                    continue
                body = c.get("body")
                if not isinstance(body, list) or not body:
                    continue
                lan = str(c.get("lan", "") or "")
                if lan and lan not in seen_langs:
                    seen_langs.append(lan)

            lan, lan_doc, segments = _select_language(content)
            if not lan:
                # No usable language for this page — skip; ``is_complete``
                # will reflect the gap.
                continue

            try:
                cid = int(entry.get("cid", 0) or 0)
            except (TypeError, ValueError):
                cid = 0
            try:
                page_index = int(entry.get("page_index", 0) or 0)
            except (TypeError, ValueError):
                page_index = 0

            pages_out.append(SubtitlePage(
                page_index=page_index,
                cid=cid,
                lan=lan,
                lan_doc=lan_doc,
                segments=segments,
            ))

        return cls(
            bvid=bvid,
            pages=pages_out,
            available_languages=seen_langs,
            source_refs=[SourceRef("video_subtitle", bvid)] if bvid else [],
            cross_refs=CrossRefs(bvid=bvid or None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": self._model_name,
            "_schema_version": self._schema_version,
            "bvid": self.bvid,
            "pages": [p.to_dict() for p in self.pages],
            "available_languages": list(self.available_languages),
            "is_complete": self.is_complete,
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoSubtitle:
        source_refs_raw = d.get("source_refs") or d.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]
        bvid = str(d.get("bvid", "") or "")
        cross_refs = CrossRefs.from_dict(d.get("cross_refs") or d.get("_cross_refs"))
        if not cross_refs.bvid and bvid:
            cross_refs.bvid = bvid

        pages_raw = d.get("pages", []) or []
        pages = [
            SubtitlePage.from_dict(p) for p in pages_raw if isinstance(p, dict)
        ]
        avail = d.get("available_languages", []) or []
        available_languages = [str(x) for x in avail if isinstance(x, str | int)]

        return cls(
            bvid=bvid,
            pages=pages,
            available_languages=available_languages,
            source_refs=source_refs,
            cross_refs=cross_refs,
        )

    # -- image protocol -- no images for subtitle data ---------------------

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        return []

    def apply_image_results(self, results: list) -> None:
        return None


# Module-level alias expected by models/__init__.py get_parser()
PARSER = VideoSubtitle
