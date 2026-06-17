# materializer -- fetch-to-store orchestration for parsing.
#
# Phase 3+ contract: reads upstream raw payloads via FetchingStore, parses
# them into typed dataclasses (one model module per spec), and persists
# them via ParsingStore's ``save_*`` dispatch.

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .specs import MODEL_ORDER, get_spec

if TYPE_CHECKING:
    from .._db import UidContext
    from ..fetching._store import FetchingStore
    from ._store import ParsingStore

logger = logging.getLogger("bili.parsing.materializer")


# Map parsing model names → ParsingStore.save_* methods. Keeps the per-handler
# code free of save-method naming knowledge so adding a new model is one entry.
_SAVE_METHODS: dict[str, str] = {
    "user_profile":   "save_user_profile",
    "video_work":     "save_video",
    "video_subtitle": "save_video_subtitle",
    "article_post":   "save_article",
    "opus_post":      "save_opus",
    "dynamic_event":  "save_dynamic",
}


# Map parsing model name → image_asset.source_kind.
_IMAGE_SOURCE_KINDS: dict[str, str] = {
    "user_profile":   "profile.face",
    "video_work":     "video.cover",
    "video_subtitle": "video.cover",  # rare; the model has no images today
    "article_post":   "article.image",
    "opus_post":      "opus.image",
    "dynamic_event":  "dynamic.image",
}


class ParsingMaterializer:
    """Materialize fetching raw payloads into stored typed objects.

    Holds three request-scoped collaborators:
      * ``parse_store`` — write side (typed object rows + task state).
      * ``fetch_store`` — read side (raw payloads from {uid}.raw.db).
      * ``ctx`` — used for ``ctx.paths.images_dir`` when downloading.
    """

    def __init__(
        self,
        ctx: UidContext,
        parse_store: ParsingStore,
        fetch_store: FetchingStore,
    ) -> None:
        self._ctx = ctx
        self._parse_store = parse_store
        self._fetch_store = fetch_store

    async def parse_model(
        self,
        uid: int,
        model_name: str,
        mode: str,
    ) -> int:
        """Parse one model and write typed objects to the parsing store."""
        spec = get_spec(model_name)
        handler = getattr(self, spec.materializer_handler)
        return await handler(uid, mode)

    async def download_images(self, uid: int) -> dict[str, Any]:
        """Download images for all parsed models and rewrite local paths.

        For each downloaded image we ALSO upsert an ``image_asset`` row via
        :meth:`ParsingStore.save_image_asset` so consumers can query asset
        bookkeeping without re-deriving it from per-object payloads.
        """
        from ._images import ImageDownloader

        downloader = ImageDownloader(base_dir=self._ctx.paths.images_dir)

        all_jobs: list[tuple[str, str]] = []
        # Track each owner's slice — (obj, model_name, source_id, count).
        job_owners: list[tuple[Any, str, str, int]] = []

        for model_name in MODEL_ORDER:
            spec = get_spec(model_name)
            parser_cls = spec.parser_cls()
            for item_id in await self._parse_store.get_existing_item_ids(model_name):
                payload = await self._read_payload_for_model(model_name, item_id)
                if payload is None:
                    continue
                obj = parser_cls.from_dict(payload)
                jobs = obj.collect_image_jobs(uid)
                all_jobs.extend(jobs)
                job_owners.append((obj, model_name, item_id, len(jobs)))

        if not all_jobs:
            return {"total": 0, "ok": 0, "skipped": 0, "failed": 0, "failed_urls": []}

        results = await self._download_missing_images(downloader, all_jobs)

        offset = 0
        for obj, model_name, source_id, count in job_owners:
            if count == 0:
                continue
            slice_results = results[offset:offset + count]
            obj.apply_image_results(slice_results)
            offset += count

            # Re-persist the dataclass so payload JSON carries `*_local`.
            save_method = getattr(self._parse_store, _SAVE_METHODS[model_name])
            await save_method(obj)

            # And record one image_asset row per result (success or failure).
            source_kind = _IMAGE_SOURCE_KINDS.get(model_name, model_name)
            for result in slice_results:
                file_path = (
                    result.local_path
                    if result.status in ("ok", "skipped")
                    else None
                )
                data = result.data if result.status in ("ok", "skipped") else None
                await self._parse_store.save_image_asset(
                    url=result.url,
                    source_kind=source_kind,
                    source_id=source_id,
                    file_path=file_path,
                    bytes=len(data) if data is not None else None,
                    data=data,
                    status=result.status,
                )

        ok = sum(1 for r in results if r.status == "ok")
        skipped = sum(1 for r in results if r.status == "skipped")
        failed = sum(1 for r in results if r.status == "failed")
        failed_urls = [r.url for r in results if r.status == "failed"]

        return {
            "total": len(results),
            "ok": ok,
            "skipped": skipped,
            "failed": failed,
            "failed_urls": failed_urls,
        }

    # -- internal helpers --------------------------------------------------

    async def _download_missing_images(
        self,
        downloader: Any,
        jobs: list[tuple[str, str]],
    ) -> list[Any]:
        """Download jobs not already present as successful DB-backed assets."""
        from ._images import ImageDownloadResult

        results: list[Any | None] = [None] * len(jobs)
        pending: list[tuple[int, str, str]] = []
        for index, (url, dest_rel) in enumerate(jobs):
            existing = await self._parse_store.get_image_asset(url)
            data = existing.get("data") if existing else None
            if (
                existing
                and existing.get("status") in ("ok", "skipped")
                and isinstance(data, bytes)
                and data is not None
            ):
                results[index] = ImageDownloadResult(
                    url=url,
                    local_path=existing.get("file_path") or dest_rel,
                    status="skipped",
                    data=data,
                )
            else:
                pending.append((index, url, dest_rel))

        if pending:
            downloaded = await downloader.download_many(
                [(url, dest_rel) for _, url, dest_rel in pending],
            )
            for (index, _, _), result in zip(pending, downloaded, strict=True):
                results[index] = result

        return [r for r in results if r is not None]

    async def _read_payload_for_model(
        self, model_name: str, item_id: str,
    ) -> dict | None:
        """Return the stored JSON payload for one (model, item_id)."""
        if model_name == "user_profile":
            try:
                return await self._parse_store.get_user_profile_payload(int(item_id))
            except (TypeError, ValueError):
                return None
        if model_name == "video_work":
            return await self._parse_store.get_video_payload(item_id)
        if model_name == "video_subtitle":
            # video_subtitle has no dedicated getter; fall back to direct SQL.
            row = await self._ctx.main.fetch_value(
                "SELECT payload FROM video_subtitle WHERE bvid = ?",
                (item_id,),
            )
            if row is None:
                return None
            return json.loads(row)
        if model_name == "article_post":
            return await self._parse_store.get_article_payload(item_id)
        if model_name == "opus_post":
            return await self._parse_store.get_opus_payload(item_id)
        if model_name == "dynamic_event":
            return await self._parse_store.get_dynamic_payload(item_id)
        raise ValueError(f"unknown parsing model: {model_name!r}")

    async def _save_typed(self, model_name: str, obj: Any) -> None:
        """Dispatch ``obj`` to the matching ``ParsingStore.save_*`` method."""
        method = getattr(self._parse_store, _SAVE_METHODS[model_name])
        await method(obj)

    async def _item_already_parsed(
        self, model_name: str, item_id: str,
    ) -> bool:
        return item_id in await self._parse_store.get_existing_item_ids(model_name)

    async def _should_skip_item(
        self, model_name: str, item_id: str, mode: str,
    ) -> bool:
        return mode == "incremental" and await self._item_already_parsed(
            model_name, item_id,
        )

    # -- per-model handlers -------------------------------------------------

    async def _parse_user_profile(self, uid: int, mode: str) -> int:
        from .models.up_profile import UpProfile

        if await self._should_skip_item("user_profile", str(uid), mode):
            logger.info("user_profile already parsed; skipped", extra={"uid": uid})
            return 0

        user_info_raw = await self._fetch_store.get_raw_payload("user_info")
        relation_info_raw = await self._fetch_store.get_raw_payload("relation_info")
        up_stat_raw = await self._fetch_store.get_raw_payload("up_stat")

        if user_info_raw is None:
            logger.info("user_info not available", extra={"uid": uid})
            return 0
        if relation_info_raw is None:
            logger.info("relation_info not available", extra={"uid": uid})
            return 0
        if up_stat_raw is None:
            logger.info("up_stat not available", extra={"uid": uid})
            return 0

        overview_stat_raw = await self._fetch_store.get_raw_payload("overview_stat")

        obj = UpProfile.from_raw(
            user_info_raw,
            relation_info_raw,
            up_stat_raw,
            overview_stat_raw,
        )
        await self._save_typed("user_profile", obj)
        logger.info("parsed user_profile", extra={"uid": uid})
        return 1

    async def _parse_video_work(self, uid: int, mode: str) -> int:
        from .models.video_detail import VideoDetail

        payloads = await self._fetch_store.list_fanout_payloads("video_detail")

        count = 0
        for bvid, raw in payloads.items():
            if not bvid or not isinstance(raw, dict):
                continue
            if await self._should_skip_item("video_work", bvid, mode):
                continue
            obj = VideoDetail.from_raw(raw)
            await self._save_typed("video_work", obj)
            count += 1

        logger.info("video works parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_video_subtitle(self, uid: int, mode: str) -> int:
        from .models.video_subtitle import VideoSubtitle

        try:
            payloads = await self._fetch_store.list_fanout_payloads("video_subtitle")
        except Exception:
            logger.warning(
                "video_subtitle fanout unavailable",
                extra={"uid": uid},
                exc_info=True,
            )
            return 0

        count = 0
        for bvid, raw in payloads.items():
            if not bvid or not isinstance(raw, dict):
                continue
            obj = VideoSubtitle.from_raw(bvid, raw)
            await self._save_typed("video_subtitle", obj)
            count += 1

        logger.info("video subtitles parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_article_posts(self, uid: int, mode: str) -> int:
        from .models.article import Article, _build_cvid_to_lists

        count = 0

        listing_raw = await self._fetch_store.get_raw_payload("articles")
        if listing_raw is None:
            logger.debug("articles endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_raw.get("pages", [])
        if not isinstance(pages, list):
            return 0

        details: dict[str, dict] = {}
        try:
            details = await self._fetch_store.list_fanout_payloads("article_detail")
        except Exception:
            logger.warning(
                "article_detail fanout unavailable",
                extra={"uid": uid},
                exc_info=True,
            )

        list_details: dict[str, dict] = {}
        try:
            list_details = await self._fetch_store.list_fanout_payloads(
                "article_list_detail",
            )
        except Exception:
            logger.warning(
                "article_list_detail fanout unavailable",
                extra={"uid": uid},
                exc_info=True,
            )

        cvid_to_lists = _build_cvid_to_lists(list_details)

        for page in pages:
            if not isinstance(page, dict):
                continue
            articles = page.get("articles", [])
            if not isinstance(articles, list):
                continue
            for list_item in articles:
                if not isinstance(list_item, dict):
                    continue
                cvid = str(list_item.get("id", ""))
                if not cvid:
                    continue

                if await self._should_skip_item("article_post", cvid, mode):
                    continue

                item = Article.from_raw(
                    list_item,
                    details.get(cvid),
                    cvid_to_lists.get(cvid, []),
                )
                await self._save_typed("article_post", item)
                count += 1

        logger.info("article posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_opus_posts(self, uid: int, mode: str) -> int:
        from .models.opus import OpusPost, _str_or_empty

        count = 0

        listing_raw = await self._fetch_store.get_raw_payload("opus")
        if listing_raw is None:
            logger.debug("opus endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_raw.get("pages", [])
        if not isinstance(pages, list):
            return 0

        details: dict[str, dict] = {}
        try:
            details = await self._fetch_store.list_fanout_payloads("opus_detail")
        except Exception:
            logger.warning(
                "opus_detail fanout unavailable",
                extra={"uid": uid},
                exc_info=True,
            )

        for page in pages:
            if not isinstance(page, dict):
                continue
            items = page.get("items", [])
            if not isinstance(items, list):
                continue
            for list_item in items:
                if not isinstance(list_item, dict):
                    continue
                opus_id_str = _str_or_empty(list_item.get("opus_id", ""))
                if not opus_id_str:
                    continue

                if await self._should_skip_item("opus_post", opus_id_str, mode):
                    continue

                item = OpusPost.from_raw(list_item, details.get(opus_id_str))
                await self._save_typed("opus_post", item)
                count += 1

        logger.info("opus posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_dynamic_events(self, uid: int, mode: str) -> int:
        from .models.dynamic import DynamicPost, _str_or_empty

        count = 0

        listing_raw = await self._fetch_store.get_raw_payload("dynamics")
        if listing_raw is None:
            logger.debug("dynamics endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_raw.get("pages", [])
        if not isinstance(pages, list):
            return 0

        for page in pages:
            if not isinstance(page, dict):
                continue
            items = page.get("items", [])
            if not isinstance(items, list):
                continue
            for raw_item in items:
                if not isinstance(raw_item, dict):
                    continue
                id_str = _str_or_empty(raw_item.get("id_str"))
                if not id_str:
                    continue

                if await self._should_skip_item("dynamic_event", id_str, mode):
                    continue

                item = DynamicPost.from_raw(raw_item)
                await self._save_typed("dynamic_event", item)
                count += 1

        logger.info("dynamic events parsed", extra={"uid": uid, "count": count})
        return count

