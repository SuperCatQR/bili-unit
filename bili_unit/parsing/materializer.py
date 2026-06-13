# materializer -- fetch-to-store orchestration for parsing.
#
# Keeps typed model modules focused on raw-shape conversion, serialisation,
# and the optional image protocol. Fetching discovery and parsing-store writes
# live here.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..fetching import EndpointStatus
from .keys import _item_key, _item_prefix
from .specs import MODEL_ORDER, get_spec

if TYPE_CHECKING:
    from ..fetching.query import Query as FetchingQuery
    from .data import ParsingDataStore

logger = logging.getLogger("bili.parsing.materializer")


class ParsingMaterializer:
    """Materialize fetching raw payloads into stored typed objects."""

    def __init__(
        self,
        data: ParsingDataStore,
        fetching_query: FetchingQuery,
    ) -> None:
        self._data = data
        self._fetch_qry = fetching_query

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
        """Download images for all parsed models and rewrite local paths."""
        from ._images import ImageDownloader

        base_dir = self._data.base / str(uid) / "images"
        downloader = ImageDownloader(base_dir=base_dir)

        all_jobs: list[tuple[str, str]] = []
        job_owners: list[tuple[Any, str, int]] = []

        for model_name in MODEL_ORDER:
            spec = get_spec(model_name)
            parser_cls = spec.parser_cls()
            items = await self._load_typed_objects(uid, model_name)
            for item_dict in items:
                obj = parser_cls.from_dict(item_dict)
                jobs = obj.collect_image_jobs(uid)
                all_jobs.extend(jobs)
                job_owners.append((obj, model_name, len(jobs)))

        if not all_jobs:
            return {"total": 0, "ok": 0, "skipped": 0, "failed": 0, "failed_urls": []}

        results = await downloader.download_many(all_jobs)

        offset = 0
        for obj, model_name, count in job_owners:
            slice_results = results[offset:offset + count]
            obj.apply_image_results(slice_results)
            offset += count
            if count > 0:
                await self._data.put(
                    _item_key(uid, model_name, obj.item_id),
                    obj.to_dict(),
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

    async def _load_typed_objects(
        self,
        uid: int,
        model_name: str,
    ) -> list[dict[str, Any]]:
        prefix = _item_prefix(uid, model_name)
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def _item_exists(self, uid: int, model_name: str, item_id: str) -> bool:
        return await self._data.get(_item_key(uid, model_name, item_id)) is not None

    async def _should_skip_item(
        self,
        uid: int,
        model_name: str,
        item_id: str,
        mode: str,
    ) -> bool:
        return mode == "incremental" and await self._item_exists(uid, model_name, item_id)

    async def _put_item(
        self,
        uid: int,
        model_name: str,
        item_id: str,
        value: dict[str, Any],
    ) -> None:
        await self._data.put(_item_key(uid, model_name, item_id), value)

    async def _parse_user_profile(self, uid: int, mode: str) -> int:
        from .models.up_profile import UpProfile

        if await self._should_skip_item(uid, "user_profile", str(uid), mode):
            logger.info("user_profile already parsed; skipped", extra={"uid": uid})
            return 0

        user_info_dto = await self._fetch_qry.get_endpoint(uid, "user_info")
        relation_info_dto = await self._fetch_qry.get_endpoint(uid, "relation_info")
        up_stat_dto = await self._fetch_qry.get_endpoint(uid, "up_stat")

        if (
            user_info_dto is None
            or user_info_dto.status != EndpointStatus.SUCCESS
            or user_info_dto.raw_payload is None
        ):
            logger.info("user_info not available", extra={"uid": uid})
            return 0

        if (
            relation_info_dto is None
            or relation_info_dto.status != EndpointStatus.SUCCESS
            or relation_info_dto.raw_payload is None
        ):
            logger.info("relation_info not available", extra={"uid": uid})
            return 0

        if (
            up_stat_dto is None
            or up_stat_dto.status != EndpointStatus.SUCCESS
            or up_stat_dto.raw_payload is None
        ):
            logger.info("up_stat not available", extra={"uid": uid})
            return 0

        overview_stat_dto = await self._fetch_qry.get_endpoint(uid, "overview_stat")
        overview_stat_raw = None
        if (
            overview_stat_dto is not None
            and overview_stat_dto.status == EndpointStatus.SUCCESS
            and overview_stat_dto.raw_payload is not None
        ):
            overview_stat_raw = overview_stat_dto.raw_payload

        obj = UpProfile.from_raw(
            user_info_dto.raw_payload,
            relation_info_dto.raw_payload,
            up_stat_dto.raw_payload,
            overview_stat_raw,
        )

        await self._put_item(uid, "user_profile", str(uid), obj.to_dict())
        logger.info("parsed user_profile", extra={"uid": uid})
        return 1

    async def _parse_video_work(self, uid: int, mode: str) -> int:
        from .models.video_detail import VideoDetail

        bvid_pairs = await self._fetch_qry.list_video_details(uid)

        count = 0
        for bvid, status in bvid_pairs:
            if status != EndpointStatus.SUCCESS:
                continue

            if await self._should_skip_item(uid, "video_work", bvid, mode):
                continue

            dto = await self._fetch_qry.get_video_detail(uid, bvid)
            if dto is None or dto.raw_payload is None:
                continue

            obj = VideoDetail.from_raw(dto.raw_payload)
            await self._put_item(uid, "video_work", bvid, obj.to_dict())
            count += 1

        logger.info("video works parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_video_detail(self, uid: int, mode: str) -> int:
        return await self._parse_video_work(uid, mode)

    async def _parse_article_posts(self, uid: int, mode: str) -> int:
        from .models.article import Article, _build_cvid_to_lists

        count = 0

        listing_dto = await self._fetch_qry.get_endpoint(uid, "articles")
        if listing_dto is None or listing_dto.raw_payload is None:
            logger.debug("articles endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_dto.raw_payload.get("pages", [])
        if not isinstance(pages, list):
            return 0

        details: dict[str, dict] = {}
        try:
            details = await self._fetch_qry.list_fanout_payloads(uid, "article_detail")
        except Exception:
            logger.warning(
                "article_detail fanout unavailable",
                extra={"uid": uid},
                exc_info=True,
            )

        list_details: dict[str, dict] = {}
        try:
            list_details = await self._fetch_qry.list_fanout_payloads(
                uid, "article_list_detail",
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

                if await self._should_skip_item(uid, "article_post", cvid, mode):
                    continue

                item = Article.from_raw(
                    list_item,
                    details.get(cvid),
                    cvid_to_lists.get(cvid, []),
                )
                await self._put_item(uid, "article_post", item.item_id, item.to_dict())
                count += 1

        logger.info("article posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_articles(self, uid: int, mode: str) -> int:
        return await self._parse_article_posts(uid, mode)

    async def _parse_opus_posts(self, uid: int, mode: str) -> int:
        from .models.opus import OpusPost, _str_or_empty

        count = 0

        listing_dto = await self._fetch_qry.get_endpoint(uid, "opus")
        if listing_dto is None or listing_dto.raw_payload is None:
            logger.debug("opus endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_dto.raw_payload.get("pages", [])
        if not isinstance(pages, list):
            return 0

        details: dict[str, dict] = {}
        try:
            details = await self._fetch_qry.list_fanout_payloads(uid, "opus_detail")
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

                if await self._should_skip_item(uid, "opus_post", opus_id_str, mode):
                    continue

                item = OpusPost.from_raw(list_item, details.get(opus_id_str))
                await self._put_item(uid, "opus_post", item.item_id, item.to_dict())
                count += 1

        logger.info("opus posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_opus(self, uid: int, mode: str) -> int:
        return await self._parse_opus_posts(uid, mode)

    async def _parse_dynamic_events(self, uid: int, mode: str) -> int:
        from .models.dynamic import DynamicPost, _str_or_empty

        count = 0

        listing_dto = await self._fetch_qry.get_endpoint(uid, "dynamics")
        if listing_dto is None or listing_dto.raw_payload is None:
            logger.debug("dynamics endpoint unavailable", extra={"uid": uid})
            return 0

        pages = listing_dto.raw_payload.get("pages", [])
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

                if await self._should_skip_item(uid, "dynamic_event", id_str, mode):
                    continue

                item = DynamicPost.from_raw(raw_item)
                await self._put_item(uid, "dynamic_event", item.item_id, item.to_dict())
                count += 1

        logger.info("dynamic events parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_dynamics(self, uid: int, mode: str) -> int:
        return await self._parse_dynamic_events(uid, mode)

    async def _parse_content_posts(self, uid: int, mode: str) -> int:
        from .models.content_post import ContentPost
        from .selectors import merge_content_posts

        candidates = [
            *await self._content_candidates_from_parsed(uid),
        ]
        if not candidates:
            candidates = await self._content_candidates_from_raw(uid)

        count = 0
        for post in merge_content_posts(candidates):
            item = ContentPost.from_dict(post.to_dict())
            if not item.content_key:
                continue
            if await self._should_skip_item(uid, "content_post", item.item_id, mode):
                continue
            await self._put_item(uid, "content_post", item.item_id, item.to_dict())
            count += 1

        logger.info("content posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _content_candidates_from_parsed(self, uid: int) -> list[Any]:
        from .models.content_post import ContentPost, CrossRefs, SourceRef, content_key_for_refs

        candidates: list[Any] = []

        for article in await self._load_typed_objects(uid, "article_post"):
            cvid = str(article.get("id") or article.get("cvid") or "")
            if not cvid:
                continue
            refs = CrossRefs.from_dict(article.get("_cross_refs") or article.get("cross_refs"))
            if not refs.cvid:
                refs.cvid = cvid
            candidates.append(ContentPost(
                content_key=content_key_for_refs(refs),
                kind="article",
                title=str(article.get("title", "") or ""),
                summary=str(article.get("summary", "") or ""),
                text=self._text_from_article(article),
                markdown=str(article.get("markdown", "") or ""),
                images=list(article.get("image_urls", []) or []),
                pub_time=article.get("pub_time") if article.get("pub_time") is not None else article.get("ctime"),
                stats=dict(article.get("stats", {}) or {}),
                source_refs=self._source_refs_from(article, [SourceRef("articles", cvid)]),
                cross_refs=refs,
            ))

        for opus in await self._load_typed_objects(uid, "opus_post"):
            opus_id = str(opus.get("id") or opus.get("opus_id") or "")
            if not opus_id:
                continue
            refs = CrossRefs.from_dict(opus.get("_cross_refs") or opus.get("cross_refs"))
            if not refs.opus_id:
                refs.opus_id = opus_id
            images: list[str] = []
            for image in opus.get("detail_images", []) or []:
                if isinstance(image, dict) and image.get("url"):
                    images.append(str(image["url"]))
            images.extend(str(url) for url in opus.get("list_images", []) or [] if url)
            if opus.get("cover"):
                images.append(str(opus["cover"]))
            candidates.append(ContentPost(
                content_key=content_key_for_refs(refs),
                kind="opus",
                title=str(opus.get("title", "") or ""),
                summary=str(opus.get("summary", "") or ""),
                text=str(opus.get("markdown") or opus.get("summary") or ""),
                markdown=str(opus.get("markdown", "") or ""),
                images=self._dedup(images),
                pub_time=opus.get("pub_time") if opus.get("pub_time") is not None else opus.get("ctime"),
                stats=dict(opus.get("stats", {}) or {}),
                source_refs=self._source_refs_from(opus, [SourceRef("opus", opus_id)]),
                cross_refs=refs,
            ))

        for event in await self._load_typed_objects(uid, "dynamic_event"):
            event_candidates = self._content_candidates_from_dynamic_event(event)
            candidates.extend(event_candidates)

        return candidates

    async def _content_candidates_from_raw(self, uid: int) -> list[Any]:
        from .selectors import select_article_posts, select_dynamic_content, select_opus_posts

        article_payload = None
        article_dto = await self._fetch_qry.get_endpoint(uid, "articles")
        if article_dto is not None and article_dto.status == EndpointStatus.SUCCESS:
            article_payload = article_dto.raw_payload

        opus_payload = None
        opus_dto = await self._fetch_qry.get_endpoint(uid, "opus")
        if opus_dto is not None and opus_dto.status == EndpointStatus.SUCCESS:
            opus_payload = opus_dto.raw_payload

        dynamics_payload = None
        dynamics_dto = await self._fetch_qry.get_endpoint(uid, "dynamics")
        if dynamics_dto is not None and dynamics_dto.status == EndpointStatus.SUCCESS:
            dynamics_payload = dynamics_dto.raw_payload

        article_details = await self._safe_fanout_payloads(uid, "article_detail")
        article_list_details = await self._safe_fanout_payloads(uid, "article_list_detail")
        opus_details = await self._safe_fanout_payloads(uid, "opus_detail")

        return [
            *select_article_posts(article_payload, article_details, article_list_details),
            *select_opus_posts(opus_payload, opus_details),
            *select_dynamic_content(dynamics_payload),
        ]

    @staticmethod
    def _source_refs_from(value: dict[str, Any], fallback: list[Any]) -> list[Any]:
        from .models.content_post import SourceRef

        raw_refs = value.get("_source_refs") or value.get("source_refs") or []
        refs = [
            SourceRef.from_dict(ref)
            for ref in raw_refs
            if isinstance(ref, SourceRef | dict)
        ]
        return refs or fallback

    @staticmethod
    def _text_from_article(article: dict[str, Any]) -> str:
        from .selectors._common import detail_text_from_content_json

        return detail_text_from_content_json(article.get("content_json")) or str(article.get("summary", "") or "")

    @staticmethod
    def _dedup(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _content_candidates_from_dynamic_event(self, event: dict[str, Any]) -> list[Any]:
        from .models.content_post import ContentPost, CrossRefs, content_key_for_refs

        dynamic_id = str(event.get("dynamic_id") or event.get("id_str") or "")
        if not dynamic_id:
            return []
        major_type = str(event.get("major_type") or "")
        refs = CrossRefs.from_dict(event.get("_cross_refs") or event.get("cross_refs"))
        if not refs.dynamic_id:
            refs.dynamic_id = dynamic_id

        # Video dynamics remain DynamicEvent target refs; they are not a readable
        # ContentPost body.
        if refs.bvid and not (refs.cvid or refs.opus_id):
            return []

        kind = "dynamic_draw"
        if refs.cvid:
            kind = "article"
        elif refs.opus_id:
            kind = "opus"
        elif event.get("forwarded_ref") or event.get("type") == "DYNAMIC_TYPE_FORWARD":
            kind = "forward"
        elif major_type == "MAJOR_TYPE_DRAW":
            kind = "dynamic_draw"

        return [ContentPost(
            content_key=content_key_for_refs(refs),
            kind=kind,
            title="",
            summary=str(event.get("text", "") or ""),
            text=str(event.get("text", "") or ""),
            markdown="",
            images=list(event.get("image_urls", []) or []),
            pub_time=event.get("pub_time") if event.get("pub_time") is not None else event.get("timestamp"),
            stats={},
            source_refs=self._source_refs_from(event, []),
            cross_refs=refs,
        )]

    async def _safe_fanout_payloads(self, uid: int, endpoint: str) -> dict[str, dict]:
        try:
            return await self._fetch_qry.list_fanout_payloads(uid, endpoint)
        except Exception:
            logger.warning(
                "fanout unavailable",
                extra={"uid": uid, "endpoint": endpoint},
                exc_info=True,
            )
            return {}
