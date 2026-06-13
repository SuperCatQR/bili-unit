# materializer -- fetch-to-store orchestration for parsing.
#
# Keeps typed model modules focused on raw-shape conversion, serialisation,
# and the optional image protocol. Fetching discovery and parsing-store writes
# live here.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..fetching import EndpointStatus
from .keys import _item_key

if TYPE_CHECKING:
    from ..fetching.query import Query as FetchingQuery
    from .data import ParsingDataStore

logger = logging.getLogger("bili.parsing.materializer")


MODEL_ORDER: tuple[str, ...] = (
    "user_profile",
    "video_detail",
    "article",
    "opus",
    "dynamic",
)


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
        if model_name == "user_profile":
            return await self._parse_user_profile(uid, mode)
        if model_name == "video_detail":
            return await self._parse_video_detail(uid, mode)
        if model_name == "article":
            return await self._parse_articles(uid, mode)
        if model_name == "opus":
            return await self._parse_opus(uid, mode)
        if model_name == "dynamic":
            return await self._parse_dynamics(uid, mode)
        raise KeyError(model_name)

    async def download_images(self, uid: int) -> dict[str, Any]:
        """Download images for all parsed models and rewrite local paths."""
        from ._images import ImageDownloader
        from .models import get_parser

        base_dir = self._data.base / str(uid) / "images"
        downloader = ImageDownloader(base_dir=base_dir)

        all_jobs: list[tuple[str, str]] = []
        job_owners: list[tuple[Any, str, int]] = []

        for model_name in MODEL_ORDER:
            parser_cls = get_parser(model_name)
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
        prefix = f"uid:{uid}:parse:{model_name}:"
        rows = await self._data.list_prefix(prefix)
        return [v for _, v in rows]

    async def _parse_user_profile(self, uid: int, mode: str) -> int:
        from .models.up_profile import UpProfile

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

        await self._data.put(_item_key(uid, "user_profile", str(uid)), obj.to_dict())
        logger.info("parsed user_profile", extra={"uid": uid})
        return 1

    async def _parse_video_detail(self, uid: int, mode: str) -> int:
        from .models.video_detail import VideoDetail

        bvid_pairs = await self._fetch_qry.list_video_details(uid)

        count = 0
        for bvid, status in bvid_pairs:
            if status != EndpointStatus.SUCCESS:
                continue

            dto = await self._fetch_qry.get_video_detail(uid, bvid)
            if dto is None or dto.raw_payload is None:
                continue

            obj = VideoDetail.from_raw(dto.raw_payload)
            await self._data.put(_item_key(uid, "video_detail", bvid), obj.to_dict())
            count += 1

        logger.info("parsed video_details", extra={"uid": uid, "count": count})
        return count

    async def _parse_articles(self, uid: int, mode: str) -> int:
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

                item = Article.from_raw(
                    list_item,
                    details.get(cvid),
                    cvid_to_lists.get(cvid, []),
                )
                await self._data.put(_item_key(uid, "article", item.item_id), item.to_dict())
                count += 1

        logger.info("articles parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_opus(self, uid: int, mode: str) -> int:
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

                item = OpusPost.from_raw(list_item, details.get(opus_id_str))
                await self._data.put(_item_key(uid, "opus", item.item_id), item.to_dict())
                count += 1

        logger.info("opus posts parsed", extra={"uid": uid, "count": count})
        return count

    async def _parse_dynamics(self, uid: int, mode: str) -> int:
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

                item = DynamicPost.from_raw(raw_item)
                await self._data.put(_item_key(uid, "dynamic", item.item_id), item.to_dict())
                count += 1

        logger.info("dynamic posts parsed", extra={"uid": uid, "count": count})
        return count
