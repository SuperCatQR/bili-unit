# video_detail -- typed model for the video_detail parsing slot.
#
# Source endpoint: video_detail (item-level fanout, per-bvid).
# Each raw payload contains an ``info`` block (video metadata, pages,
# stats, owner) and a ``tags`` list.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

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

    _model_name: str = "video_detail"

    bvid: str = ""
    aid: int | None = None
    title: str = ""
    desc: str = ""
    duration: int = 0
    ctime: int | None = None
    pubdate: int | None = None
    pic: str = ""
    pages: list[PageInfo] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    stat: VideoStat = field(default_factory=VideoStat)
    owner: OwnerInfo = field(default_factory=OwnerInfo)
    rights: dict = field(default_factory=dict)
    subtitle: dict = field(default_factory=dict)
    label: dict = field(default_factory=dict)

    # -- local assets (filled after image download) --------------------------
    pic_local: str = ""

    # -----------------------------------------------------------------------
    # Common interface
    # -----------------------------------------------------------------------

    @property
    def item_id(self) -> str:
        """Stable string ID for this item."""
        return self.bvid

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

        return cls(
            bvid=info.get("bvid", ""),
            aid=info.get("aid"),
            title=info.get("title", ""),
            desc=info.get("desc", ""),
            duration=info.get("duration", 0),
            ctime=info.get("ctime"),
            pubdate=info.get("pubdate"),
            pic=info.get("pic", ""),
            pages=pages,
            tags=tags,
            stat=stat,
            owner=owner,
            rights=info.get("rights", {}) if isinstance(info.get("rights"), dict) else {},
            subtitle=info.get("subtitle", {}) if isinstance(info.get("subtitle"), dict) else {},
            label=info.get("label", {}) if isinstance(info.get("label"), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        return {
            "_model_name": self._model_name,
            "bvid": self.bvid,
            "aid": self.aid,
            "title": self.title,
            "desc": self.desc,
            "duration": self.duration,
            "ctime": self.ctime,
            "pubdate": self.pubdate,
            "pic": self.pic,
            "pages": [p.to_dict() for p in self.pages],
            "tags": list(self.tags),
            "stat": self.stat.to_dict(),
            "owner": self.owner.to_dict(),
            "rights": dict(self.rights),
            "subtitle": dict(self.subtitle),
            "label": dict(self.label),
            "pic_local": self.pic_local,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoDetail:
        """Deserialize from JSON dict."""
        return cls(
            bvid=d.get("bvid", ""),
            aid=d.get("aid"),
            title=d.get("title", ""),
            desc=d.get("desc", ""),
            duration=d.get("duration", 0),
            ctime=d.get("ctime"),
            pubdate=d.get("pubdate"),
            pic=d.get("pic", ""),
            pages=[PageInfo.from_dict(p) for p in d.get("pages", [])],
            tags=d.get("tags", []),
            stat=VideoStat.from_dict(d.get("stat", {})),
            owner=OwnerInfo.from_dict(d.get("owner", {})),
            rights=d.get("rights", {}),
            subtitle=d.get("subtitle", {}),
            label=d.get("label", {}),
            pic_local=d.get("pic_local", ""),
        )

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image downloading."""
        if self.pic:
            return [(self.pic, f"video/{self.bvid}_cover.jpg")]
        return []

    def apply_image_results(self, results: list) -> None:
        """Fill in *_local fields after image download."""
        if results:
            r = results[0]
            if r.status in ("ok", "skipped"):
                self.pic_local = r.local_path

# Module-level alias expected by models/__init__.py get_parser()
PARSER = VideoDetail
