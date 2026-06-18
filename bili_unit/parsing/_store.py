# bili_unit.parsing._store — SQLite-backed write store for the parsing stage.
#
# Replaces the old ``ParsingDataStore`` (file-directory JSON KV) with one
# semantic write method per typed parsing model.  Each ``save_*`` decomposes
# the dataclass into a few commonly-queried typed columns plus a ``payload``
# JSON blob that round-trips the full ``to_dict()`` output.
#
# Conventions:
#   * INSERT OR REPLACE — re-parse always wins; the parser does not need to
#     distinguish first-write from overwrite.
#   * ``payload = json.dumps(obj.to_dict(), ensure_ascii=False)`` — Chinese
#     text stays readable when consumers ``SELECT payload FROM ...``.
#   * Timestamps default to ``int(time.time() * 1000)`` if the caller does not
#     supply one.
#   * pubdate conversions: bilibili-api delivers ``pubdate`` / ``ctime`` /
#     ``pub_time`` in seconds-epoch.  This module always promotes them to
#     ms-epoch via ``* 1000`` when filling a ``pubdate_ms`` column.  The
#     individual save methods document this on a per-field basis.

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from .._db import UidContext

if TYPE_CHECKING:
    from .models.article import Article
    from .models.dynamic import DynamicPost
    from .models.opus import OpusPost
    from .models.up_profile import UpProfile
    from .models.video_detail import VideoDetail
    from .models.video_subtitle import VideoSubtitle

logger = logging.getLogger("bili.parsing.store")


# Models that map 1:1 to a content table on the read side.
# 'video_work' is a parser-side alias for the underlying ``video`` table.
_MODEL_TABLE: dict[str, tuple[str, str]] = {
    # model_name : (table, primary_key_col)
    "user_profile":   ("user_profile",   "uid"),
    "video_work":     ("video",          "bvid"),
    "video_subtitle": ("video_subtitle", "bvid"),
    "article_post":   ("article",        "cvid"),
    "opus_post":      ("opus_post",      "opus_id"),
    "dynamic_event":  ("dynamic_event",  "dynamic_id"),
}

# Pre-built SQL for the four read/write operations that address a single model.
# Using constants avoids f-string table/column interpolation in hot paths and
# makes SQL auditable at module load time.
_LIST_PK_SQL: dict[str, str] = {
    "user_profile":   "SELECT uid FROM user_profile",
    "video_work":     "SELECT bvid FROM video",
    "video_subtitle": "SELECT bvid FROM video_subtitle",
    "article_post":   "SELECT cvid FROM article",
    "opus_post":      "SELECT opus_id FROM opus_post",
    "dynamic_event":  "SELECT dynamic_id FROM dynamic_event",
}

_GET_PARSED_AT_SQL: dict[str, str] = {
    "user_profile":   "SELECT parsed_at_ms FROM user_profile WHERE uid = ?",
    "video_work":     "SELECT parsed_at_ms FROM video WHERE bvid = ?",
    "video_subtitle": "SELECT parsed_at_ms FROM video_subtitle WHERE bvid = ?",
    "article_post":   "SELECT parsed_at_ms FROM article WHERE cvid = ?",
    "opus_post":      "SELECT parsed_at_ms FROM opus_post WHERE opus_id = ?",
    "dynamic_event":  "SELECT parsed_at_ms FROM dynamic_event WHERE dynamic_id = ?",
}

_UPDATE_PAYLOAD_SQL: dict[str, str] = {
    "user_profile":   "UPDATE user_profile SET payload = ? WHERE uid = ?",
    "video_work":     "UPDATE video SET payload = ? WHERE bvid = ?",
    "video_subtitle": "UPDATE video_subtitle SET payload = ? WHERE bvid = ?",
    "article_post":   "UPDATE article SET payload = ? WHERE cvid = ?",
    "opus_post":      "UPDATE opus_post SET payload = ? WHERE opus_id = ?",
    "dynamic_event":  "UPDATE dynamic_event SET payload = ? WHERE dynamic_id = ?",
}

_GET_PAYLOAD_SQL: dict[str, str] = {
    "user_profile":   "SELECT payload FROM user_profile WHERE uid = ?",
    "video_work":     "SELECT payload FROM video WHERE bvid = ?",
    "video_subtitle": "SELECT payload FROM video_subtitle WHERE bvid = ?",
    "article_post":   "SELECT payload FROM article WHERE cvid = ?",
    "opus_post":      "SELECT payload FROM opus_post WHERE opus_id = ?",
    "dynamic_event":  "SELECT payload FROM dynamic_event WHERE dynamic_id = ?",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _payload(obj: Any) -> str:
    """Serialize a dataclass via its ``to_dict()`` to a UTF-8 JSON string."""
    return json.dumps(obj.to_dict(), ensure_ascii=False)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8"), usedforsecurity=False).hexdigest()


def _s_to_ms(seconds_epoch: int | None) -> int | None:
    """Convert a seconds-epoch int to ms-epoch (None passes through)."""
    if seconds_epoch is None:
        return None
    return int(seconds_epoch) * 1000


class ParsingStore:
    """SQLite-backed write store for the parsing stage.

    Writes typed parsed objects to their respective tables in the main DB.
    Each save method takes the dataclass directly and decomposes it into
    typed columns + a ``payload`` JSON blob escape hatch.
    """

    def __init__(self, ctx: UidContext) -> None:
        self._ctx = ctx

    # -- typed object writes ------------------------------------------------

    async def save_user_profile(self, profile: UpProfile) -> None:
        """Upsert a UpProfile row.

        Column mapping (UpProfile.to_dict() → user_profile):
          * uid       ← mid
          * name      ← name
          * sign      ← sign
          * face_url  ← face_url        (raw upstream field is ``face``)
          * level     ← level
          * follower  ← social["follower"]
          * following ← social["following"]
        """
        if profile.mid is None:
            raise ValueError("UpProfile.mid is required to persist a user_profile row")
        social = profile.social if isinstance(profile.social, dict) else {}
        await self._ctx.main.execute(
            """
            INSERT OR REPLACE INTO user_profile
                (uid, name, sign, face_url, level, follower, following,
                 payload, parsed_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(profile.mid),
                profile.name,
                profile.sign,
                profile.face_url,
                int(profile.level or 0),
                int(social.get("follower", 0) or 0),
                int(social.get("following", 0) or 0),
                _payload(profile),
                _now_ms(),
            ),
        )

    async def save_video(self, video: VideoDetail) -> None:
        """Upsert a video row plus its video_page rows in one transaction.

        Column mapping (VideoDetail.to_dict() → video):
          * bvid        ← bvid
          * aid         ← aid
          * title       ← title
          * description ← description
          * cover_url   ← cover_url
          * duration_s  ← duration_s                     (already seconds)
          * pubdate_ms  ← pubdate_ms                     (already ms; converted in from_raw)
          * view_count  ← stat["view"]
          * danmaku, reply, favorite, coin, share        ← stat[…] (passthrough)
          * like_count  ← stat["like"]

        Existing video_page rows are upserted in place.  Only pages that
        disappeared from the latest parse are deleted, so stable page rows do
        not cascade-delete subtitle/ASR page results.
        """
        if not video.bvid:
            raise ValueError("VideoDetail.bvid is required to persist a video row")

        stat = video.stat
        payload = _payload(video)
        now_ms = _now_ms()
        pubdate_ms = video.pubdate_ms

        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                """
                INSERT INTO video
                    (bvid, aid, title, description, cover_url,
                     duration_s, pubdate_ms,
                     view_count, danmaku, reply, favorite, coin, share, like_count,
                     payload, parsed_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    aid = excluded.aid,
                    title = excluded.title,
                    description = excluded.description,
                    cover_url = excluded.cover_url,
                    duration_s = excluded.duration_s,
                    pubdate_ms = excluded.pubdate_ms,
                    view_count = excluded.view_count,
                    danmaku = excluded.danmaku,
                    reply = excluded.reply,
                    favorite = excluded.favorite,
                    coin = excluded.coin,
                    share = excluded.share,
                    like_count = excluded.like_count,
                    payload = excluded.payload,
                    parsed_at_ms = excluded.parsed_at_ms
                """,
                (
                    video.bvid,
                    video.aid,
                    video.title,
                    video.description,
                    video.cover_url,
                    int(video.duration_s or 0),
                    pubdate_ms,
                    int(stat.view or 0),
                    int(stat.danmaku or 0),
                    int(stat.reply or 0),
                    int(stat.favorite or 0),
                    int(stat.coin or 0),
                    int(stat.share or 0),
                    int(stat.like or 0),
                    payload,
                    now_ms,
                ),
            ),
        ]
        page_numbers = tuple(range(1, len(video.pages) + 1))
        if page_numbers:
            placeholders = ", ".join("?" for _ in page_numbers)
            statements.append(
                (
                    f"DELETE FROM video_page WHERE bvid = ? "
                    f"AND page_no NOT IN ({placeholders})",
                    (video.bvid, *page_numbers),
                ),
            )
        else:
            statements.append(
                (
                    "DELETE FROM video_page WHERE bvid = ?",
                    (video.bvid,),
                ),
            )
        for idx, page in enumerate(video.pages, start=1):
            statements.append(
                (
                    """
                    INSERT INTO video_page
                        (bvid, page_no, cid, part, duration_s)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bvid, page_no) DO UPDATE SET
                        cid = excluded.cid,
                        part = excluded.part,
                        duration_s = excluded.duration_s
                    """,
                    (
                        video.bvid,
                        idx,
                        page.cid,
                        page.part,
                        int(page.duration or 0),
                    ),
                ),
            )

        await self._ctx.main.run_transaction(statements)

    async def save_video_subtitle(self, sub: VideoSubtitle) -> None:
        """Upsert a video_subtitle row.

        Column mapping (VideoSubtitle.to_dict() → video_subtitle):
          * bvid          ← bvid
          * has_bilibili_human_uploaded_or_official_subtitle
                          ← any selected page with ``is_ai=False`` (derived)
          * has_bilibili_platform_ai_generated_subtitle
                          ← selected AI page or available ``ai-*`` language

        ``video_subtitle.payload`` is the canonical parsed subtitle object.
        ``video_subtitle_page`` and ``video_subtitle_segment`` are derived
        query tables rebuilt from that payload on every save.  Bilibili
        platform AI subtitles are persisted in main DB with explicit source
        flags, but ASR does not treat them as a trusted shortcut.
        """
        if not sub.bvid:
            raise ValueError("VideoSubtitle.bvid is required to persist a row")

        payload = sub.to_dict()
        now_ms = _now_ms()
        has_bilibili_human_uploaded_or_official_subtitle = any(
            p.lan and not p.is_ai and not p.lan.startswith("ai-")
            for p in sub.pages
        )
        has_bilibili_platform_ai_generated_subtitle = any(
            p.lan and (p.is_ai or p.lan.startswith("ai-"))
            for p in sub.pages
        ) or any(lan.startswith("ai-") for lan in sub.available_languages)

        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                """
                INSERT INTO video_subtitle
                    (bvid, has_bilibili_human_uploaded_or_official_subtitle,
                     has_bilibili_platform_ai_generated_subtitle, payload,
                     parsed_at_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    has_bilibili_human_uploaded_or_official_subtitle =
                        excluded.has_bilibili_human_uploaded_or_official_subtitle,
                    has_bilibili_platform_ai_generated_subtitle =
                        excluded.has_bilibili_platform_ai_generated_subtitle,
                    payload = excluded.payload,
                    parsed_at_ms = excluded.parsed_at_ms
                """,
                (
                    sub.bvid,
                    1 if has_bilibili_human_uploaded_or_official_subtitle else 0,
                    1 if has_bilibili_platform_ai_generated_subtitle else 0,
                    json.dumps(payload, ensure_ascii=False),
                    now_ms,
                ),
            ),
            (
                "DELETE FROM video_subtitle_page WHERE bvid = ?",
                (sub.bvid,),
            ),
        ]
        for page in sub.pages:
            if not page.lan.strip():
                continue
            page_no = int(page.page_index) + 1
            is_platform_ai = bool(page.is_ai or page.lan.startswith("ai-"))
            page_text = " ".join(s.content for s in page.segments)
            statements.append(
                (
                    """
                    INSERT INTO video_subtitle_page
                        (bvid, page_no, bilibili_video_page_index,
                         bilibili_video_page_cid,
                         selected_bilibili_subtitle_language_code,
                         selected_bilibili_subtitle_language_name,
                         is_selected_bilibili_subtitle_platform_ai_generated,
                         selected_bilibili_subtitle_text,
                         subtitle_segment_count, parsed_at_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sub.bvid,
                        page_no,
                        int(page.page_index),
                        int(page.cid or 0),
                        page.lan,
                        page.lan_doc,
                        1 if is_platform_ai else 0,
                        page_text,
                        len(page.segments),
                        now_ms,
                    ),
                ),
            )
            for segment_no, segment in enumerate(page.segments, start=1):
                statements.append(
                    (
                        """
                        INSERT INTO video_subtitle_segment
                            (bvid, page_no, segment_no,
                             bilibili_subtitle_start_seconds,
                             bilibili_subtitle_end_seconds,
                             bilibili_subtitle_duration_seconds,
                             bilibili_subtitle_segment_text)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sub.bvid,
                            page_no,
                            segment_no,
                            float(segment.start),
                            float(segment.end),
                            max(0.0, float(segment.end) - float(segment.start)),
                            segment.content,
                        ),
                    ),
                )
        await self._ctx.main.run_transaction(statements)

    async def save_article(self, art: Article) -> None:
        """Upsert an article row.

        Column mapping (Article.to_dict() → article):
          * cvid       ← id
          * title      ← title
          * summary    ← summary
          * pubdate_ms ← ctime * 1000             (s → ms conversion)
          * view_count ← stats.view
          * like_count ← stats.like
          * reply      ← stats.reply
        """
        if not art.id:
            raise ValueError("Article.id is required to persist an article row")
        await self._ctx.main.execute(
            """
            INSERT OR REPLACE INTO article
                (cvid, title, summary, pubdate_ms,
                 view_count, like_count, reply,
                 payload, parsed_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                art.id,
                art.title,
                art.summary,
                _s_to_ms(art.ctime),
                int(art.stats.view or 0),
                int(art.stats.like or 0),
                int(art.stats.reply or 0),
                _payload(art),
                _now_ms(),
            ),
        )

    async def save_opus(self, opus: OpusPost) -> None:
        """Upsert an opus_post row.

        Column mapping (OpusPost.to_dict() → opus_post):
          * opus_id    ← id
          * pubdate_ms ← ctime * 1000              (s → ms conversion;
                                                    OpusPost stores pub_time
                                                    under ``ctime`` after
                                                    ``from_raw``)
        """
        if not opus.id:
            raise ValueError("OpusPost.id is required to persist an opus_post row")
        await self._ctx.main.execute(
            """
            INSERT OR REPLACE INTO opus_post
                (opus_id, pubdate_ms, payload, parsed_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                opus.id,
                _s_to_ms(opus.ctime),
                _payload(opus),
                _now_ms(),
            ),
        )

    async def save_dynamic(self, dyn: DynamicPost) -> None:
        """Upsert a dynamic_event row.

        Column mapping (DynamicPost.to_dict() → dynamic_event):
          * dynamic_id ← dynamic_id (or id_str fallback)
          * type       ← type
          * pubdate_ms ← timestamp * 1000          (s → ms conversion;
                                                    DynamicPost.timestamp is
                                                    pub_ts in seconds)
        """
        item_id = dyn.dynamic_id or dyn.id_str
        if not item_id:
            raise ValueError("DynamicPost requires id_str/dynamic_id")
        await self._ctx.main.execute(
            """
            INSERT OR REPLACE INTO dynamic_event
                (dynamic_id, type, pubdate_ms, payload, parsed_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_id,
                dyn.type,
                _s_to_ms(dyn.timestamp),
                _payload(dyn),
                _now_ms(),
            ),
        )

    # -- image asset bookkeeping --------------------------------------------

    async def save_image_asset(
        self,
        *,
        url: str,
        source_kind: str,
        source_id: str,
        file_path: str | None,
        bytes: int | None,
        status: str,
        data: bytes | None = None,
        downloaded_at_ms: int | None = None,
    ) -> None:
        """Upsert an image_asset row keyed by ``md5(url)``."""
        await self._ctx.main.execute(
            """
            INSERT OR REPLACE INTO image_asset
                (url_hash, source_kind, source_id, url,
                 file_path, bytes, data, status, downloaded_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _md5_hex(url),
                source_kind,
                source_id,
                url,
                file_path,
                bytes,
                data,
                status,
                downloaded_at_ms if downloaded_at_ms is not None else _now_ms(),
            ),
        )

    async def list_image_assets(self) -> list[dict]:
        """Return every image_asset metadata row as a list of dicts."""
        rows = await self._ctx.main.fetch_all(
            """
            SELECT url_hash, source_kind, source_id, url,
                   file_path, bytes, status, downloaded_at_ms
              FROM image_asset
             ORDER BY downloaded_at_ms ASC, url_hash ASC
            """,
        )
        return [dict(r) for r in rows]

    async def get_image_asset(self, url: str) -> dict | None:
        """Return one image_asset row, including BLOB data, by source URL."""
        row = await self._ctx.main.fetch_one(
            """
            SELECT url_hash, source_kind, source_id, url,
                   file_path, bytes, data, status, downloaded_at_ms
              FROM image_asset
             WHERE url_hash = ?
            """,
            (_md5_hex(url),),
        )
        return None if row is None else dict(row)

    # -- read-side: only what the parser needs to decide skip/redo ---------

    async def get_existing_item_ids(self, model: str) -> set[str]:
        """Return the set of stored item ids for ``model``.

        ``model`` accepts the parser-side names: ``user_profile``,
        ``video_work`` (maps to the ``video`` table), ``video_subtitle``,
        ``article_post``, ``opus_post``, ``dynamic_event``.
        """
        try:
            sql = _LIST_PK_SQL[model]
        except KeyError as exc:
            raise ValueError(f"unknown parsing model: {model!r}") from exc
        rows = await self._ctx.main.fetch_all(sql)
        # uid is INTEGER everywhere else it's TEXT; coerce uniformly to str
        return {str(r[0]) for r in rows}

    async def get_item_parsed_at_ms(self, model: str, item_id: str) -> int | None:
        """Return parsed_at_ms for one parsed item, or None if absent."""
        try:
            sql = _GET_PARSED_AT_SQL[model]
        except KeyError as exc:
            raise ValueError(f"unknown parsing model: {model!r}") from exc
        row = await self._ctx.main.fetch_one(
            sql,
            (item_id,),
        )
        if row is None:
            return None
        return int(row["parsed_at_ms"])

    async def update_model_payload(
        self,
        model: str,
        item_id: str,
        payload: dict,
    ) -> None:
        """Update only a parsed object's payload JSON.

        Image localization mutates payload escape-hatch fields, but it is not
        a fresh parse of upstream raw data.  Preserving ``parsed_at_ms`` keeps
        incremental parsing tied to raw payload freshness.
        """
        try:
            sql = _UPDATE_PAYLOAD_SQL[model]
        except KeyError as exc:
            raise ValueError(f"unknown parsing model: {model!r}") from exc
        await self._ctx.main.execute(
            sql,
            (json.dumps(payload, ensure_ascii=False), item_id),
        )

    async def get_video_payload(self, bvid: str) -> dict | None:
        """Return the JSON payload for ``bvid`` (or None if absent)."""
        return await self._read_payload("video", "bvid", bvid)

    async def get_video_subtitle_payload(self, bvid: str) -> dict | None:
        """Return the parsed VideoSubtitle JSON payload for ``bvid`` (or None).

        Used by the processing stage's audio pipeline to short-circuit ASR
        when an upstream subtitle is already available for every page.
        """
        return await self._read_payload("video_subtitle", "bvid", bvid)

    async def list_video_page_work_items(self) -> dict[str, list[dict]]:
        """Return ASR work metadata from the main DB.

        The ASR stage consumes parsed ``video`` / ``video_page`` rows instead
        of raw ``video_detail`` fanout payloads. The returned mapping is keyed
        by bvid and each page dict carries the same shape the audio runner has
        historically used: zero-based ``page_index``, ``cid``, ``part`` and
        ``duration`` in seconds.
        """
        rows = await self._ctx.main.fetch_all(
            """
            SELECT v.bvid,
                   COALESCE(p.page_no, 1) AS page_no,
                   p.cid,
                   p.part,
                   COALESCE(p.duration_s, v.duration_s, 0) AS duration_s
              FROM video v
              LEFT JOIN video_page p ON p.bvid = v.bvid
             ORDER BY v.bvid ASC, COALESCE(p.page_no, 1) ASC
            """,
        )
        out: dict[str, list[dict]] = {}
        for row in rows:
            bvid = str(row["bvid"])
            page_no = int(row["page_no"] or 1)
            out.setdefault(bvid, []).append({
                "page_index": max(0, page_no - 1),
                "cid": row["cid"] or 0,
                "duration": row["duration_s"] or 0,
                "part": row["part"] or "",
            })
        return out

    async def video_subtitle_is_complete(self, bvid: str) -> bool:
        """Return True iff a parsed video_subtitle row for ``bvid`` exists
        and has a selected language (``lan``) on every page.

        A bvid with no row returns False; a row whose pages list is empty or
        missing returns False; a row where any page lacks ``lan`` returns
        False. Mirrors the completeness rule the audio short-circuit relies
        on.
        """
        payload = await self.get_video_subtitle_payload(bvid)
        if not isinstance(payload, dict):
            return False
        pages = payload.get("bilibili_subtitle_pages", payload.get("pages"))
        if not isinstance(pages, list) or not pages:
            return False
        for p in pages:
            if not isinstance(p, dict):
                return False
            if not (
                p.get("selected_bilibili_subtitle_language_code")
                or p.get("lan")
            ):
                return False
        return True

    async def get_user_profile_payload(self, uid: int) -> dict | None:
        return await self._read_payload("user_profile", "uid", uid)

    async def get_article_payload(self, cvid: str) -> dict | None:
        return await self._read_payload("article", "cvid", cvid)

    async def get_opus_payload(self, opus_id: str) -> dict | None:
        return await self._read_payload("opus_post", "opus_id", opus_id)

    async def get_dynamic_payload(self, dynamic_id: str) -> dict | None:
        return await self._read_payload("dynamic_event", "dynamic_id", dynamic_id)

    async def _read_payload(
        self, table: str, pk: str, item_id: Any,
    ) -> dict | None:
        # Reverse-lookup the model key for this (table, pk) so we hit the
        # template in _GET_PAYLOAD_SQL rather than building SQL from inputs.
        _table_pk_to_model = {(t, p): m for m, (t, p) in _MODEL_TABLE.items()}
        model_key = _table_pk_to_model.get((table, pk))
        if model_key is None:
            raise ValueError(
                f"_read_payload called with unknown (table, pk) pair: ({table!r}, {pk!r})"
            )
        sql = _GET_PAYLOAD_SQL[model_key]
        raw = await self._ctx.main.fetch_value(
            sql,
            (item_id,),
        )
        if raw is None:
            return None
        return json.loads(raw)

    # -- task state ---------------------------------------------------------

    async def init_task(self, models: list[str]) -> None:
        """Insert (or merge) the parsing stage_task row.

        Idempotent: re-calling with the same (or extended) ``models`` list
        preserves any existing per-model status/count entries.  Newly-listed
        models are added with PENDING/0; models already present are kept.
        """
        existing = await self.get_task()
        now_ms = _now_ms()
        if existing is None:
            payload: dict[str, Any] = {
                "models": {m: {"status": "PENDING", "count": 0} for m in models},
                "images": None,
            }
            await self._ctx.main.execute(
                """
                INSERT INTO stage_task
                    (stage, status, payload, created_at_ms, updated_at_ms)
                VALUES ('parsing', ?, ?, ?, ?)
                """,
                ("PENDING", json.dumps(payload, ensure_ascii=False), now_ms, now_ms),
            )
            return

        # Merge: don't overwrite per-model status if already set.
        models_block = existing.get("models")
        if not isinstance(models_block, dict):
            models_block = {}
        for m in models:
            if m not in models_block:
                models_block[m] = {"status": "PENDING", "count": 0}
        existing["models"] = models_block
        existing.setdefault("images", None)
        await self._ctx.main.execute(
            """
            UPDATE stage_task
               SET payload = ?, updated_at_ms = ?
             WHERE stage = 'parsing'
            """,
            (json.dumps(existing, ensure_ascii=False), now_ms),
        )

    async def update_task_model_status(
        self, model: str, status: str, count: int = 0,
    ) -> None:
        """Set the (status, count) entry for one model in the parsing task payload.

        Read-modify-write of the JSON ``payload`` column.  Parsing is a
        single-writer stage per uid (the materializer iterates models
        sequentially), so the read and write don't need to share one lock
        acquisition — the asyncio.Lock inside Connection still prevents
        coroutine-level interleaving of the two statements.
        """
        payload = await self.get_task()
        if payload is None:
            return
        models_block = payload.get("models")
        if not isinstance(models_block, dict):
            models_block = {}
            payload["models"] = models_block
        entry = models_block.get(model)
        if not isinstance(entry, dict):
            entry = {"status": "PENDING", "count": 0}
            models_block[model] = entry
        entry["status"] = status
        entry["count"] = int(count)
        await self._write_task_payload(payload)

    async def update_task_images(self, images_summary: dict) -> None:
        """Replace the ``images`` block in the parsing task payload."""
        payload = await self.get_task()
        if payload is None:
            return
        payload["images"] = dict(images_summary)
        await self._write_task_payload(payload)

    async def update_task_status(self, status: str) -> None:
        """Set the parsing task status (no payload mutation)."""
        now = _now_ms()
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                """
                UPDATE stage_task
                   SET status = ?, updated_at_ms = ?
                 WHERE stage = 'parsing'
                """,
                (status, now),
            ),
        ]
        if status not in {"PENDING", "RUNNING"}:
            statements.append(
                (
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("last_parsed_at_ms", str(now)),
                ),
            )
        await self._ctx.main.run_transaction(statements)

    async def get_task(self) -> dict | None:
        """Return the parsing stage_task payload (decoded), or None."""
        raw = await self._ctx.main.fetch_value(
            "SELECT payload FROM stage_task WHERE stage = 'parsing'",
        )
        if raw is None:
            return None
        return json.loads(raw)

    async def _write_task_payload(self, payload: dict) -> None:
        await self._ctx.main.execute(
            """
            UPDATE stage_task
               SET payload = ?, updated_at_ms = ?
             WHERE stage = 'parsing'
            """,
            (json.dumps(payload, ensure_ascii=False), _now_ms()),
        )


__all__ = ["ParsingStore"]
