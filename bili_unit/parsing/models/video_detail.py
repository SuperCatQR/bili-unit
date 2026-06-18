# video_detail -- typed model for the video_detail parsing slot.
#
# Source endpoint: video_detail (item-level fanout, per-bvid).
# Each raw payload contains an ``info`` block (video metadata, pages,
# stats, owner) and a ``tags`` list.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ._refs import CrossRefs, SourceRef

logger = logging.getLogger("bili.parsing.models.video_detail")


# ---------------------------------------------------------------------------
# Nested helper dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PageInfo:
    """A single video part / page (P1, P2, ...)."""

    cid: int | None = None
    part: str = ""
    duration: int = 0
    dimension: dict = field(default_factory=dict)
    first_frame: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "part": self.part,
            "duration": self.duration,
            "dimension": dict(self.dimension),
            "first_frame": self.first_frame,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageInfo:
        return cls(
            cid=d.get("cid"),
            part=d.get("part", ""),
            duration=d.get("duration", 0),
            dimension=d.get("dimension", {}),
            first_frame=d.get("first_frame", ""),
        )


@dataclass
class VideoStat:
    """Aggregate engagement statistics for a video."""

    view: int = 0
    danmaku: int = 0
    reply: int = 0
    favorite: int = 0
    coin: int = 0
    share: int = 0
    like: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "danmaku": self.danmaku,
            "reply": self.reply,
            "favorite": self.favorite,
            "coin": self.coin,
            "share": self.share,
            "like": self.like,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoStat:
        return cls(
            view=d.get("view", 0),
            danmaku=d.get("danmaku", 0),
            reply=d.get("reply", 0),
            favorite=d.get("favorite", 0),
            coin=d.get("coin", 0),
            share=d.get("share", 0),
            like=d.get("like", 0),
        )


@dataclass
class OwnerInfo:
    """Video uploader identity."""

    mid: int | None = None
    name: str = ""
    face: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mid": self.mid,
            "name": self.name,
            "face": self.face,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OwnerInfo:
        return cls(
            mid=d.get("mid"),
            name=d.get("name", ""),
            face=d.get("face", ""),
        )


# ---------------------------------------------------------------------------
# Main model
# ---------------------------------------------------------------------------

@dataclass
class VideoDetail:
    """Typed representation of a single Bilibili video's detail data."""

    _model_name: str = "video_work"
    _schema_version: int = 1

    bvid: str = ""
    aid: int | None = None
    title: str = ""
    description: str = ""
    duration_s: int = 0
    ctime: int | None = None
    pubdate_ms: int | None = None
    cover_url: str = ""
    pages: list[PageInfo] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    stat: VideoStat = field(default_factory=VideoStat)
    owner: OwnerInfo = field(default_factory=OwnerInfo)
    rights: dict = field(default_factory=dict)
    subtitle: dict = field(default_factory=dict)
    label: dict = field(default_factory=dict)

    # -- local assets (filled after image download) --------------------------
    pic_local: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    # -----------------------------------------------------------------------
    # Common interface
    # -----------------------------------------------------------------------

    @property
    def item_id(self) -> str:
        """Stable string ID for this item."""
        return self.bvid

    @property
    def is_complete(self) -> bool:
        """True iff the ``video_detail`` endpoint contributed and a bvid is set."""
        return any(ref.endpoint == "video_detail" for ref in self.source_refs) and bool(self.bvid)

    @classmethod
    def from_raw(cls, raw: dict) -> VideoDetail:
        """Create from a raw fetching dict.

        ``raw`` is the full payload from fetching: ``{"info": {...}, "tags": [...]}``
        """
        info = raw.get("info", {})
        tags_raw = raw.get("tags", [])

        # Pages
        pages: list[PageInfo] = []
        for p in info.get("pages", []):
            if isinstance(p, dict):
                pages.append(
                    PageInfo(
                        cid=p.get("cid"),
                        part=p.get("part", ""),
                        duration=p.get("duration", 0),
                        dimension=p.get("dimension", {}) if isinstance(p.get("dimension"), dict) else {},
                        first_frame=p.get("first_frame", ""),
                    )
                )

        # Tags -- extract tag_name from each tag dict
        tags: list[str] = []
        for t in tags_raw:
            if isinstance(t, dict):
                name = t.get("tag_name", "")
                if name:
                    tags.append(name)
            elif isinstance(t, str) and t:
                tags.append(t)

        # Stat
        stat_raw = info.get("stat", {})
        stat = VideoStat(
            view=stat_raw.get("view", 0),
            danmaku=stat_raw.get("danmaku", 0),
            reply=stat_raw.get("reply", 0),
            favorite=stat_raw.get("favorite", 0),
            coin=stat_raw.get("coin", 0),
            share=stat_raw.get("share", 0),
            like=stat_raw.get("like", 0),
        ) if isinstance(stat_raw, dict) else VideoStat()

        # Owner
        owner_raw = info.get("owner", {})
        owner = OwnerInfo(
            mid=owner_raw.get("mid"),
            name=owner_raw.get("name", ""),
            face=owner_raw.get("face", ""),
        ) if isinstance(owner_raw, dict) else OwnerInfo()

        # pubdate: raw upstream delivers seconds-epoch; store as ms-epoch
        raw_pubdate = info.get("pubdate")
        pubdate_ms = (int(raw_pubdate) * 1000) if raw_pubdate is not None else None

        return cls(
            bvid=info.get("bvid", ""),
            aid=info.get("aid"),
            title=info.get("title", ""),
            description=info.get("desc", ""),   # upstream raw field is 'desc'
            duration_s=info.get("duration", 0),
            ctime=info.get("ctime"),
            pubdate_ms=pubdate_ms,
            cover_url=info.get("pic", ""),       # upstream raw field is 'pic'
            pages=pages,
            tags=tags,
            stat=stat,
            owner=owner,
            rights=info.get("rights", {}) if isinstance(info.get("rights"), dict) else {},
            subtitle=info.get("subtitle", {}) if isinstance(info.get("subtitle"), dict) else {},
            label=info.get("label", {}) if isinstance(info.get("label"), dict) else {},
            source_refs=[SourceRef("video_detail", info.get("bvid", ""))] if info.get("bvid") else [],
            cross_refs=CrossRefs(bvid=info.get("bvid", "") or None),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        return {
            "_model_name": self._model_name,
            "_schema_version": self._schema_version,
            "bvid": self.bvid,
            "aid": self.aid,
            "title": self.title,
            "description": self.description,
            "duration_s": self.duration_s,
            "ctime": self.ctime,
            "pubdate_ms": self.pubdate_ms,
            "cover_url": self.cover_url,
            "pages": [p.to_dict() for p in self.pages],
            "tags": list(self.tags),
            "stat": self.stat.to_dict(),
            "owner": self.owner.to_dict(),
            "rights": dict(self.rights),
            "subtitle": dict(self.subtitle),
            "label": dict(self.label),
            "pic_local": self.pic_local,
            "is_complete": self.is_complete,
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoDetail:
        """Deserialize from JSON dict."""
        source_refs_raw = d.get("source_refs") or d.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]
        bvid = d.get("bvid", "")
        cross_refs = CrossRefs.from_dict(d.get("cross_refs") or d.get("_cross_refs"))
        if not cross_refs.bvid and bvid:
            cross_refs.bvid = bvid
        return cls(
            bvid=bvid,
            aid=d.get("aid"),
            title=d.get("title", ""),
            # prefer new keys; fall back to old keys for legacy payloads
            description=d.get("description") or d.get("desc", ""),
            duration_s=d.get("duration_s") if d.get("duration_s") is not None else d.get("duration", 0),
            ctime=d.get("ctime"),
            pubdate_ms=(
                d["pubdate_ms"]
                if d.get("pubdate_ms") is not None
                # Legacy payload key 'pubdate' is seconds-epoch — convert to ms
                # so consumers always see a millisecond value on this field.
                else (d["pubdate"] * 1000 if d.get("pubdate") is not None else None)
            ),
            cover_url=d.get("cover_url") or d.get("pic", ""),
            pages=[PageInfo.from_dict(p) for p in d.get("pages", [])],
            tags=d.get("tags", []),
            stat=VideoStat.from_dict(d.get("stat", {})),
            owner=OwnerInfo.from_dict(d.get("owner", {})),
            rights=d.get("rights", {}),
            subtitle=d.get("subtitle", {}),
            label=d.get("label", {}),
            pic_local=d.get("pic_local", ""),
            source_refs=source_refs,
            cross_refs=cross_refs,
        )

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image downloading."""
        if self.cover_url:
            return [(self.cover_url, f"video/{self.bvid}_cover.jpg")]
        return []

    def apply_image_results(self, results: list) -> None:
        """Fill in *_local fields after image download."""
        if results:
            r = results[0]
            if r.status in ("ok", "skipped"):
                self.pic_local = r.local_path

# Module-level alias expected by models/__init__.py get_parser()
PARSER = VideoDetail
VideoWork = VideoDetail
