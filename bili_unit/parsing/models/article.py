# article — Article typed model for the parsing layer.
#
# Source endpoints:
#   articles           (listing, paginated)
#   article_detail     (per-cvid enrichment)
#   article_list_detail (per-rlid readlist roster)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bili.parsing.models.article")


# ---------------------------------------------------------------------------
# Sub-dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ArticleStats:
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
    def from_dict(cls, d: dict[str, Any]) -> ArticleStats:
        return cls(
            view=d.get("view", 0),
            favorite=d.get("favorite", 0),
            like=d.get("like", 0),
            reply=d.get("reply", 0),
            share=d.get("share", 0),
            coin=d.get("coin", 0),
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> ArticleStats:
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


@dataclass
class ReadListMeta:
    rlid: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"rlid": self.rlid, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReadListMeta:
        return cls(rlid=str(d.get("rlid", "")), name=str(d.get("name", "")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dedup_urls(*sources: Any) -> list[str]:
    """Merge multiple URL sources into a deduplicated list, skipping non-strings."""
    seen: set[str] = set()
    result: list[str] = []
    for src in sources:
        if isinstance(src, str):
            urls = [src]
        elif isinstance(src, list):
            urls = src
        else:
            continue
        for u in urls:
            if isinstance(u, str) and u and u not in seen:
                seen.add(u)
                result.append(u)
    return result


def _build_cvid_to_lists(
    list_details: dict[str, dict],
) -> dict[str, list[dict]]:
    """Reverse-index article_list_detail payloads to {cvid -> [{rlid, name}]}.

    Each list_detail payload has the shape:
        {"list": {"id": 1, "name": "..."}, "articles": [{"id": 123}, ...]}

    Returns a mapping from cvid (as string) to a list of {rlid, name} dicts.
    """
    cvid_map: dict[str, list[dict]] = {}
    for rlid, payload in list_details.items():
        if not isinstance(payload, dict):
            continue
        list_info = payload.get("list") or {}
        list_name = str(list_info.get("name", "")) if isinstance(list_info, dict) else ""
        articles = payload.get("articles") or []
        if not isinstance(articles, list):
            continue
        for art in articles:
            if not isinstance(art, dict):
                continue
            cvid = str(art.get("id", ""))
            if not cvid:
                continue
            cvid_map.setdefault(cvid, []).append({
                "rlid": str(rlid),
                "name": list_name,
            })
    return cvid_map


# ---------------------------------------------------------------------------
# Main dataclass
# ---------------------------------------------------------------------------

@dataclass
class Article:
    _model_name: str = "article"

    id: str = ""
    title: str = ""
    summary: str = ""
    image_urls: list[str] = field(default_factory=list)
    stats: ArticleStats = field(default_factory=ArticleStats)
    ctime: int | None = None
    lists: list[ReadListMeta] = field(default_factory=list)
    markdown: str = ""
    content_json: list = field(default_factory=list)
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
        list_membership: list[dict],
    ) -> Article:
        """Build an Article from raw fetching payloads.

        Args:
            list_item: One article dict from the articles listing page.
            detail: Per-cvid article_detail raw_payload (or None).
            list_membership: List of {"rlid": "...", "name": "..."} dicts
                from the cvid-to-lists reverse index.
        """
        cvid = str(list_item.get("id", ""))

        # Image URLs: merge image_urls + origin_image_urls + banner_url
        image_urls = _dedup_urls(
            list_item.get("image_urls", []),
            list_item.get("origin_image_urls", []),
            list_item.get("banner_url"),
        )

        # Stats
        stats = ArticleStats.from_raw(list_item.get("stats"))

        # ctime
        raw_ctime = list_item.get("ctime")
        ctime = int(raw_ctime) if raw_ctime is not None else None

        # Readlist membership
        lists = [
            ReadListMeta(rlid=str(m.get("rlid", "")), name=str(m.get("name", "")))
            for m in list_membership
            if isinstance(m, dict)
        ]

        # Detail enrichment
        markdown = ""
        content_json: list = []
        if detail and isinstance(detail, dict):
            md = detail.get("markdown")
            if isinstance(md, str):
                markdown = md
            cj = detail.get("content_json")
            if isinstance(cj, list):
                content_json = cj

        return cls(
            id=cvid,
            title=str(list_item.get("title", "") or ""),
            summary=str(list_item.get("summary", "") or ""),
            image_urls=image_urls,
            stats=stats,
            ctime=ctime,
            lists=lists,
            markdown=markdown,
            content_json=content_json,
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_model_name": self._model_name,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "image_urls": list(self.image_urls),
            "stats": self.stats.to_dict(),
            "ctime": self.ctime,
            "lists": [rl.to_dict() for rl in self.lists],
            "markdown": self.markdown,
            "content_json": self.content_json,
            "image_locals": list(self.image_locals),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Article:
        stats_d = d.get("stats")
        stats = ArticleStats.from_dict(stats_d) if isinstance(stats_d, dict) else ArticleStats()

        lists_raw = d.get("lists", [])
        lists = [
            ReadListMeta.from_dict(rl) for rl in lists_raw if isinstance(rl, dict)
        ]

        return cls(
            id=str(d.get("id", "")),
            title=str(d.get("title", "")),
            summary=str(d.get("summary", "")),
            image_urls=list(d.get("image_urls", [])),
            stats=stats,
            ctime=d.get("ctime"),
            lists=lists,
            markdown=str(d.get("markdown", "") or ""),
            content_json=list(d.get("content_json", []) or []),
            image_locals=list(d.get("image_locals", []) or []),
        )

    # -- image pipeline ------------------------------------------------------

    def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
        """Return [(url, dest_rel), ...] for image download."""
        return [
            (url, f"article/{self.id}_{i:02d}.jpg")
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

PARSER = Article
