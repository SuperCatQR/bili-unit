# up_profile -- typed model for the user_profile parsing slot.
#
# Aggregates 4 fetching endpoints (user_info, relation_info, up_stat,
# overview_stat) into a single UpProfile dataclass per uid.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .content_post import CrossRefs, SourceRef

logger = logging.getLogger("bili.parsing.models.up_profile")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_vip(user_info: dict) -> dict[str, Any]:
    """Extract normalised VIP block from a user_info payload."""
    vip = user_info.get("vip")
    if not isinstance(vip, dict):
        return {"type": 0, "status": 0, "label": ""}

    label = vip.get("label")
    if isinstance(label, dict):
        label_text = label.get("text", "")
    elif isinstance(label, str):
        label_text = label
    else:
        label_text = ""

    return {
        "type": vip.get("type", 0),
        "status": vip.get("status", 0),
        "label": label_text,
    }


def _extract_social(relation_info: dict) -> dict[str, int]:
    """Extract social counts from a relation_info payload."""
    return {
        "following": relation_info.get("following", 0),
        "follower": relation_info.get("follower", 0),
        "whisper": relation_info.get("whisper", 0),
        "black": relation_info.get("black", 0),
    }


def _nested_view(d: dict, key: str) -> int:
    """Return the ``view`` int from a possibly-nested sub-dict."""
    sub = d.get(key)
    if isinstance(sub, dict):
        return sub.get("view", 0)
    if isinstance(sub, int):
        return sub
    return 0


def _extract_stats(up_stat: dict) -> dict[str, int]:
    """Extract aggregate stats from an up_stat payload."""
    return {
        "archive_view": _nested_view(up_stat, "archive"),
        "article_view": _nested_view(up_stat, "article"),
        "likes": up_stat.get("likes", 0),
    }


def _count(d: dict, *names: str) -> int:
    """Return the first int value found among candidate key names."""
    for name in names:
        val = d.get(name)
        if isinstance(val, int):
            return val
    return 0


def _extract_overview(
    overview_stat: dict | None,
) -> dict[str, int] | None:
    """Extract content-count overview from an optional overview_stat payload."""
    if not overview_stat:
        return None
    return {
        "video_count": _count(overview_stat, "video", "video_count"),
        "article_count": _count(overview_stat, "article", "article_count"),
        "opus_count": _count(overview_stat, "opus", "opus_count"),
    }


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class UpProfile:
    """Typed representation of a Bilibili user's profile data."""

    _model_name: str = "user_profile"
    _schema_version: int = 1

    # -- identity (from user_info) -------------------------------------------
    mid: int | None = None
    name: str = ""
    sex: str = ""
    sign: str = ""
    avatar: str = ""
    birthday: str = ""
    level: int = 0
    jointime: int = 0
    vip: dict = field(default_factory=lambda: {"type": 0, "status": 0, "label": ""})

    # -- social (from relation_info) -----------------------------------------
    social: dict = field(default_factory=lambda: {
        "following": 0, "follower": 0, "whisper": 0, "black": 0,
    })

    # -- stats (from up_stat) ------------------------------------------------
    stats: dict = field(default_factory=lambda: {
        "archive_view": 0, "article_view": 0, "likes": 0,
    })

    # -- overview (from overview_stat, optional) -----------------------------
    overview: dict | None = None

    # -- local assets (filled after image download) --------------------------
    avatar_local: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    cross_refs: CrossRefs = field(default_factory=CrossRefs)

    # -----------------------------------------------------------------------
    # Common interface
    # -----------------------------------------------------------------------

    @property
    def item_id(self) -> str:
        """Stable string ID for this item."""
        return str(self.mid)

    @property
    def is_complete(self) -> bool:
        """True iff the 3 required endpoints all contributed a source_ref.

        ``overview_stat`` is optional and does not affect completeness.
        """
        seen = {ref.endpoint for ref in self.source_refs}
        return {"user_info", "relation_info", "up_stat"} <= seen

    @classmethod
    def from_raw(
        cls,
        user_info: dict,
        relation_info: dict,
        up_stat: dict,
        overview_stat: dict | None = None,
    ) -> UpProfile:
        """Create from raw fetching dict(s)."""
        mid = user_info.get("mid")
        source_refs = [
            SourceRef("user_info", str(mid or "")),
            SourceRef("relation_info", str(mid or "")),
            SourceRef("up_stat", str(mid or "")),
        ]
        if overview_stat:
            source_refs.append(SourceRef("overview_stat", str(mid or "")))

        return cls(
            mid=user_info.get("mid"),
            name=user_info.get("name", ""),
            sex=user_info.get("sex", ""),
            sign=user_info.get("sign", ""),
            avatar=user_info.get("face", ""),
            birthday=user_info.get("birthday", ""),
            level=user_info.get("level", 0),
            jointime=user_info.get("jointime", 0),
            vip=_extract_vip(user_info),
            social=_extract_social(relation_info),
            stats=_extract_stats(up_stat),
            overview=_extract_overview(overview_stat),
            source_refs=source_refs,
            cross_refs=CrossRefs(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        return {
            "_model_name": self._model_name,
            "_schema_version": self._schema_version,
            "mid": self.mid,
            "name": self.name,
            "sex": self.sex,
            "sign": self.sign,
            "avatar": self.avatar,
            "birthday": self.birthday,
            "level": self.level,
            "jointime": self.jointime,
            "vip": dict(self.vip),
            "social": dict(self.social),
            "stats": dict(self.stats),
            "overview": dict(self.overview) if self.overview is not None else None,
            "avatar_local": self.avatar_local,
            "is_complete": self.is_complete,
            "_source_refs": [ref.to_dict() for ref in self.source_refs],
            "_cross_refs": self.cross_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UpProfile:
        """Deserialize from JSON dict."""
        source_refs_raw = d.get("source_refs") or d.get("_source_refs") or []
        source_refs = [
            SourceRef.from_dict(ref)
            for ref in source_refs_raw
            if isinstance(ref, SourceRef | dict)
        ]
        return cls(
            mid=d.get("mid"),
            name=d.get("name", ""),
            sex=d.get("sex", ""),
            sign=d.get("sign", ""),
            avatar=d.get("avatar", ""),
            birthday=d.get("birthday", ""),
            level=d.get("level", 0),
            jointime=d.get("jointime", 0),
            vip=d.get("vip", {"type": 0, "status": 0, "label": ""}),
            social=d.get("social", {
                "following": 0, "follower": 0, "whisper": 0, "black": 0,
            }),
            stats=d.get("stats", {
                "archive_view": 0, "article_view": 0, "likes": 0,
            }),
            overview=d.get("overview"),
            avatar_local=d.get("avatar_local", ""),
            source_refs=source_refs,
            cross_refs=CrossRefs.from_dict(d.get("cross_refs") or d.get("_cross_refs")),
        )

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image downloading."""
        if self.avatar:
            return [(self.avatar, "avatar.jpg")]
        return []

    def apply_image_results(self, results: list) -> None:
        """Fill in *_local fields after image download."""
        if results:
            r = results[0]
            if r.status in ("ok", "skipped"):
                self.avatar_local = r.local_path

# Module-level alias expected by models/__init__.py get_parser()
PARSER = UpProfile
UserProfile = UpProfile
