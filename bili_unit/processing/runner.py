# runner — orchestrates the processing task across pipelines.
#
# Per docs/design/processing.md §10:
#   Phase 0  扫描     读取 fetching task；按 endpoint 粒度生成工作项；增量/全量决策
#   Phase 1  分发执行 入队 transform_queue + audio_queue；启动 worker pools；并行处理
#   Phase 2  收尾     汇总状态；更新 processing task；清理 temp
#
# Concurrency control: asyncio.Queue with maxsize from
# BILI_PROCESSING_QUEUE_MAXSIZE. Workers exit on sentinel (None).

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    AudioError,
    ProcessingError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from .audio._converter import convert_single
from .audio._downloader import AudioDownloader
from .keys import _proc_key, _progress_key, _task_key
from .task import PipelineEntry, ProcessingTaskValue
from .transform import HANDLERS, get_handler
from .transform._base import WorkItem

if TYPE_CHECKING:
    from ..fetching.query import Query as FetchingQuery
    from .audio._asr_backend import ASRBackend
    from .data import ProcessingDataStore
    from .env import ProcessingEnv
    from .error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.runner")


_TRANSFORM = "transform"
_AUDIO = "audio"


class ProcessingRunner:
    """Orchestrate transform / audio pipelines for a uid."""

    def __init__(
        self,
        data: ProcessingDataStore,
        error: ProcessingErrorStore,
        temp_dir: str,
        fetching_query: FetchingQuery,
        settings: ProcessingEnv,
        asr_backend: ASRBackend | None = None,
    ) -> None:
        self._data = data
        self._error = error
        self._temp_dir = temp_dir
        self._fetch_qry = fetching_query
        self._settings = settings
        self._asr_backend = asr_backend

    # -- public API --------------------------------------------------------

    async def run(
        self,
        uid: int,
        pipelines: list[str] | None = None,
        item_types: list[str] | None = None,
        mode: str = "incremental",
    ) -> ProcessingTaskStatus:
        """Run the processing task for a uid.

        Args:
            uid: target user.
            pipelines: subset of {"transform", "audio"}; default all.
            item_types: subset of registered transform item_types; default all.
            mode: "incremental" (default) or "full".
        """
        if mode not in ("incremental", "full"):
            raise ValueError(f"unknown mode: {mode!r}")
        active_pipelines = self._select_pipelines(pipelines)
        active_item_types = self._select_item_types(item_types)

        logger.info(
            "processing_start",
            extra={"uid": uid, "mode": mode, "pipelines": active_pipelines,
                   "item_types": active_item_types},
        )

        # Phase 0 — load / create task value, scan work items
        tv = await self._load_or_init_task(uid, active_pipelines)
        tv.status = ProcessingTaskStatus.RUNNING
        await self._save_task(tv)

        # Phase 1 — transform pipeline
        if _TRANSFORM in active_pipelines:
            await self._run_transform(uid, tv, active_item_types, mode)

        # Phase 1 — audio pipeline
        if _AUDIO in active_pipelines:
            await self._run_audio(uid, tv, mode)

        # Phase 2 — finalise
        tv = await self._reload_task(uid) or tv
        tv.status = self._derive_task_status(tv)
        await self._save_task(tv)

        # Phase 2 — cleanup temp
        await self._cleanup_temp(uid)

        logger.info(
            "processing_completed",
            extra={"uid": uid, "status": tv.status.value},
        )
        return tv.status

    # -- transform pipeline ------------------------------------------------

    async def _run_transform(
        self,
        uid: int,
        tv: ProcessingTaskValue,
        item_types: list[str],
        mode: str,
    ) -> None:
        """Phase-1 transform: scan → enqueue → workers → rollup."""
        entry = tv.pipelines.setdefault(_TRANSFORM, PipelineEntry())
        entry.status = ProcessingPipelineStatus.RUNNING
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=entry.items,
        )

        # 1. discover work items per item_type
        rollup: dict[str, dict[str, int]] = {}
        all_items: list[WorkItem] = []
        for it in item_types:
            handler = get_handler(it)
            if handler is None:
                continue
            try:
                discovered = await self._discover_items(uid, handler, mode)
            except ProcessingError as exc:
                logger.warning(
                    "transform_discovery_failed",
                    extra={"uid": uid, "item_type": it, "error": str(exc)},
                )
                rollup[it] = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
                continue

            ready, skipped = discovered
            rollup[it] = {
                "total": len(ready) + skipped,
                "completed": 0,
                "failed": 0,
                "skipped": skipped,
            }
            all_items.extend(ready)

        entry.items = rollup
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=entry.items,
        )

        # 2. write progress markers (initial)
        for it, counts in rollup.items():
            await self._write_progress(uid, _TRANSFORM, it, counts, done=False)

        # 3. queue + worker pool
        if all_items:
            await self._dispatch_workers(uid, all_items, rollup)

        # 4. final pipeline status
        entry.status = self._derive_pipeline_status(rollup)
        await self._data.update_task_pipeline(
            _task_key(uid), _TRANSFORM, entry.status.value, items=rollup,
        )
        for it, counts in rollup.items():
            await self._write_progress(uid, _TRANSFORM, it, counts, done=True)

    async def _discover_items(
        self,
        uid: int,
        handler: Any,
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Phase-0 discovery for a single transform handler.

        Returns (ready_items, skipped_count). 'skipped' counts items already
        SUCCESS in incremental mode.

        Per §10.1 fetching consumption rule: only emit items when the source
        endpoint is SUCCESS for uid-level, or PARTIAL_ITEM/SUCCESS for
        item-level fan-out (only the SUCCESS items are enumerated).
        """
        from ..fetching import EndpointStatus as _EpStatus  # late import

        raw_payloads: dict[str, dict] = {}
        for ep in handler.source_endpoints:
            if ep == "video_detail":
                # item-level fan-out: enumerate only SUCCESS items.
                vd_pairs = await self._fetch_qry.list_video_details(uid)
                if not vd_pairs:
                    logger.info(
                        "transform_endpoint_unavailable",
                        extra={"uid": uid, "item_type": handler.item_type,
                               "endpoint": ep},
                    )
                    return [], 0
                items: list[WorkItem] = []
                for bvid, status in vd_pairs:
                    if status != _EpStatus.SUCCESS:
                        continue
                    item_dto = await self._fetch_qry.get_video_detail(uid, bvid)
                    if item_dto is None or item_dto.raw_payload is None:
                        continue
                    items.extend(handler.extract_items(
                        {"video_detail": item_dto.raw_payload},
                    ))
                return await self._filter_ready(uid, handler, items, mode)

            # uid-level endpoint
            ep_dto = await self._fetch_qry.get_endpoint(uid, ep)
            if ep_dto is None or not ep_dto.available:
                logger.info(
                    "transform_endpoint_unavailable",
                    extra={"uid": uid, "item_type": handler.item_type, "endpoint": ep},
                )
                return [], 0
            raw_payloads[ep] = ep_dto.raw_payload or {}

        items = handler.extract_items(raw_payloads)
        return await self._filter_ready(uid, handler, items, mode)

    async def _filter_ready(
        self,
        uid: int,
        handler: Any,
        items: list[WorkItem],
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Apply incremental skip rule: SUCCESS already-stored items are skipped."""
        if mode == "full":
            return items, 0
        # incremental: skip items already SUCCESS, retry FAILED, run new
        ready: list[WorkItem] = []
        skipped = 0
        for item in items:
            existing = await self._data.get(_proc_key(uid, item.item_type, item.item_id))
            if existing is None:
                ready.append(item)
                continue
            status = existing.get("status")
            if status == ProcessingItemStatus.SUCCESS.value:
                skipped += 1
                continue
            # FAILED / SKIPPED / PROCESSING / PENDING → retry once
            ready.append(item)
        return ready, skipped

    async def _dispatch_workers(
        self,
        uid: int,
        items: list[WorkItem],
        rollup: dict[str, dict[str, int]],
    ) -> None:
        """Run transform workers over ``items``; updates rollup in-place."""
        worker_count = max(1, int(self._settings.bili_processing_transform_workers))
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, int(self._settings.bili_processing_queue_maxsize)),
        )
        rollup_lock = asyncio.Lock()

        async def producer() -> None:
            for item in items:
                await queue.put(item)
            for _ in range(worker_count):
                await queue.put(None)  # sentinel

        async def worker(idx: int) -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                handler = get_handler(item.item_type)
                if handler is None:
                    async with rollup_lock:
                        bucket = rollup.setdefault(
                            item.item_type,
                            {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                        )
                        bucket["failed"] += 1
                    continue
                ok = await self._process_one(uid, handler, item)
                async with rollup_lock:
                    bucket = rollup.setdefault(
                        item.item_type,
                        {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                    )
                    if ok:
                        bucket["completed"] += 1
                    else:
                        bucket["failed"] += 1

        await asyncio.gather(
            producer(),
            *[worker(i) for i in range(worker_count)],
        )

    async def _process_one(
        self,
        uid: int,
        handler: Any,
        item: WorkItem,
    ) -> bool:
        key = _proc_key(uid, item.item_type, item.item_id)
        max_retries = self._settings.bili_processing_max_retries
        retry_delays = self._settings.get_retry_delays()

        for attempt in range(max_retries + 1):
            try:
                result = handler.transform(item)
            except Exception as exc:  # noqa: BLE001 — retry / record below
                retryable = self._is_retryable(exc)

                if retryable and attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    logger.info(
                        "transform_item_retry",
                        extra={"uid": uid, "item_id": item.item_id,
                               "retry": attempt + 1, "delay_s": delay,
                               "error": str(exc)},
                    )
                    await self._error.record(
                        exc, uid=uid, pipeline=_TRANSFORM,
                        item_type=item.item_type, item_id=item.item_id,
                        retryable="true",
                        detail={"retry_count": attempt + 1},
                    )
                    now = int(time.time() * 1000)
                    await self._data.put(key, {
                        "uid": uid, "pipeline": _TRANSFORM,
                        "item_type": item.item_type, "item_id": item.item_id,
                        "status": ProcessingItemStatus.FAILED.value,
                        "result": None,
                        "source_endpoints": list(handler.source_endpoints),
                        "processed_at": now,
                        "retry_count": attempt + 1,
                    })
                    await asyncio.sleep(delay)
                    continue

                # Non-retryable or exhausted
                # Mark retryable="false" because no more retries will happen:
                # either the error type is non-retryable, or retries are spent.
                retryable_str = "true" if (retryable and attempt < max_retries - 1) else "false"
                await self._error.record(
                    exc, uid=uid, pipeline=_TRANSFORM,
                    item_type=item.item_type, item_id=item.item_id,
                    retryable=retryable_str,
                    detail={"retry_count": attempt + 1} if attempt > 0 else None,
                )
                now = int(time.time() * 1000)
                await self._data.put(key, {
                    "uid": uid, "pipeline": _TRANSFORM,
                    "item_type": item.item_type, "item_id": item.item_id,
                    "status": ProcessingItemStatus.FAILED.value,
                    "result": None,
                    "source_endpoints": list(handler.source_endpoints),
                    "processed_at": now,
                    "retry_count": attempt + 1 if attempt > 0 else 0,
                })
                return False

            now = int(time.time() * 1000)
            await self._data.put(key, {
                "uid": uid, "pipeline": _TRANSFORM,
                "item_type": item.item_type, "item_id": item.item_id,
                "status": ProcessingItemStatus.SUCCESS.value,
                "result": result,
                "source_endpoints": list(handler.source_endpoints),
                "processed_at": now,
            })
            return True
        return False  # pragma: no cover — loop always returns inside

    # -- audio pipeline ----------------------------------------------------

    async def _run_audio(
        self,
        uid: int,
        tv: ProcessingTaskValue,
        mode: str,
    ) -> None:
        """Phase-1 audio: discover → enqueue → workers → rollup."""
        entry = tv.pipelines.setdefault(_AUDIO, PipelineEntry())
        entry.status = ProcessingPipelineStatus.RUNNING
        await self._data.update_task_pipeline(
            _task_key(uid), _AUDIO, entry.status.value, items=entry.items,
        )

        # 1. discover audio work items (one per bvid)
        try:
            audio_items, skipped = await self._discover_audio_items(uid, mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audio_discovery_failed",
                extra={"uid": uid, "error": str(exc)},
            )
            audio_items, skipped = [], 0

        rollup: dict[str, dict[str, int]] = {
            "transcription": {
                "total": len(audio_items) + skipped,
                "completed": 0,
                "failed": 0,
                "skipped": skipped,
            },
        }
        entry.items = rollup
        await self._data.update_task_pipeline(
            _task_key(uid), _AUDIO, entry.status.value, items=rollup,
        )

        # 2. write progress marker (initial)
        await self._write_progress(uid, _AUDIO, "transcription", rollup["transcription"], done=False)

        # 3. resolve credential for CDN downloads
        credential = None
        if audio_items:
            try:
                from ..fetching.auth import get_credential
                credential = await get_credential()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audio_credential_unavailable",
                    extra={"uid": uid, "error": str(exc)},
                )

        # 4. queue + audio worker pool
        if audio_items:
            await self._dispatch_audio_workers(uid, audio_items, rollup, credential)

        # 5. final pipeline status
        entry.status = self._derive_pipeline_status(rollup)
        await self._data.update_task_pipeline(
            _task_key(uid), _AUDIO, entry.status.value, items=rollup,
        )
        await self._write_progress(uid, _AUDIO, "transcription", rollup["transcription"], done=True)

    async def _discover_audio_items(
        self,
        uid: int,
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Discover audio work items from video_detail.

        Each bvid produces one WorkItem carrying its page list.
        Returns (ready_items, skipped_count).
        """
        from ..fetching import EndpointStatus as _EpStatus

        vd_pairs = await self._fetch_qry.list_video_details(uid)
        if not vd_pairs:
            return [], 0

        items: list[WorkItem] = []
        for bvid, status in vd_pairs:
            if status != _EpStatus.SUCCESS:
                continue
            dto = await self._fetch_qry.get_video_detail(uid, bvid)
            if dto is None or dto.raw_payload is None:
                continue

            info = dto.raw_payload.get("info", {})
            pages = info.get("pages", [])
            if not pages:
                continue

            # Build page metadata list for the audio worker.
            page_list = []
            for i, page in enumerate(pages):
                page_list.append({
                    "page_index": i,
                    "cid": page.get("cid", 0),
                    "duration": page.get("duration", info.get("duration", 0)),
                    "part": page.get("part", ""),
                })

            items.append(WorkItem(
                item_type="audio",
                item_id=bvid,
                item_data={"bvid": bvid, "pages": page_list},
            ))

        return await self._filter_audio_ready(uid, items, mode)

    async def _filter_audio_ready(
        self,
        uid: int,
        items: list[WorkItem],
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Apply incremental skip rule for audio items."""
        if mode == "full":
            return items, 0
        ready: list[WorkItem] = []
        skipped = 0
        for item in items:
            existing = await self._data.get(_proc_key(uid, "audio", item.item_id))
            if existing is None:
                ready.append(item)
                continue
            status = existing.get("status")
            if status == ProcessingItemStatus.SUCCESS.value:
                skipped += 1
                continue
            ready.append(item)
        return ready, skipped

    async def _dispatch_audio_workers(
        self,
        uid: int,
        items: list[WorkItem],
        rollup: dict[str, dict[str, int]],
        credential: Any,
    ) -> None:
        """Run audio workers; updates rollup in-place."""
        worker_count = max(1, int(self._settings.bili_processing_audio_workers))
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, int(self._settings.bili_processing_queue_maxsize)),
        )
        rollup_lock = asyncio.Lock()

        async def producer() -> None:
            for item in items:
                await queue.put(item)
            for _ in range(worker_count):
                await queue.put(None)

        async def worker(idx: int) -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                try:
                    ok = await self._process_audio_one(uid, item, credential)
                except Exception as exc:  # noqa: BLE001 — safety net
                    logger.error(
                        "audio_worker_unexpected_error",
                        extra={"uid": uid, "bvid": item.item_id,
                               "error": str(exc)},
                    )
                    ok = False
                async with rollup_lock:
                    bucket = rollup["transcription"]
                    if ok:
                        bucket["completed"] += 1
                    else:
                        bucket["failed"] += 1

        await asyncio.gather(
            producer(),
            *[worker(i) for i in range(worker_count)],
        )

    async def _process_audio_one(
        self,
        uid: int,
        item: WorkItem,
        credential: Any,
    ) -> bool:
        """Process a single bvid through the audio pipeline with retry.

        Wraps ``_do_audio_work`` in a retry loop following the same
        configurable max_retries + exponential backoff strategy as transform.
        """
        bvid = item.item_id
        key = _proc_key(uid, "audio", bvid)
        max_retries = self._settings.bili_processing_max_retries
        retry_delays = self._settings.get_retry_delays()

        for attempt in range(max_retries + 1):
            try:
                result = await self._do_audio_work(uid, item, credential)
            except Exception as exc:  # noqa: BLE001 — retry / record below
                retryable = self._is_retryable(exc)

                if retryable and attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    logger.info(
                        "audio_item_retry",
                        extra={"uid": uid, "bvid": bvid,
                               "retry": attempt + 1, "delay_s": delay,
                               "error": str(exc)},
                    )
                    await self._error.record(
                        exc, uid=uid, pipeline=_AUDIO,
                        item_type="transcription", item_id=bvid,
                        retryable="true",
                        detail={"retry_count": attempt + 1},
                    )
                    now = int(time.time() * 1000)
                    await self._data.put(key, {
                        "uid": uid, "pipeline": _AUDIO,
                        "item_type": "transcription", "item_id": bvid,
                        "status": ProcessingItemStatus.FAILED.value,
                        "result": None,
                        "source_endpoints": ["video_detail"],
                        "processed_at": now,
                        "retry_count": attempt + 1,
                    })
                    await asyncio.sleep(delay)
                    continue

                # Non-retryable or exhausted
                logger.warning(
                    "audio_item_failed",
                    extra={"uid": uid, "bvid": bvid,
                           "retry_count": attempt + 1, "error": str(exc)},
                )
                # Mark retryable="false" because no more retries will happen.
                retryable_str = "true" if (retryable and attempt < max_retries - 1) else "false"
                await self._error.record(
                    exc, uid=uid, pipeline=_AUDIO,
                    item_type="transcription", item_id=bvid,
                    retryable=retryable_str,
                    detail={"retry_count": attempt + 1} if attempt > 0 else None,
                )
                now = int(time.time() * 1000)
                await self._data.put(key, {
                    "uid": uid, "pipeline": _AUDIO,
                    "item_type": "transcription", "item_id": bvid,
                    "status": ProcessingItemStatus.FAILED.value,
                    "result": None,
                    "source_endpoints": ["video_detail"],
                    "processed_at": now,
                    "retry_count": attempt + 1 if attempt > 0 else 0,
                })
                return False

            now = int(time.time() * 1000)
            await self._data.put(key, {
                "uid": uid, "pipeline": _AUDIO,
                "item_type": "transcription", "item_id": bvid,
                "status": ProcessingItemStatus.SUCCESS.value,
                "result": result,
                "source_endpoints": ["video_detail"],
                "processed_at": now,
            })
            return True
        return False  # pragma: no cover — loop always returns inside

    async def _do_audio_work(
        self,
        uid: int,
        item: WorkItem,
        credential: Any,
    ) -> dict[str, Any]:
        """Process all pages for a single bvid.  Returns result dict.

        Raises on any error; temp files are always cleaned up in ``finally``.
        """
        bvid = item.item_id
        pages = item.item_data.get("pages", [])

        temp_base = Path(self._temp_dir) / str(uid) / "audio" / bvid
        temp_base.mkdir(parents=True, exist_ok=True)

        ffmpeg_setting = self._settings.bili_processing_ffmpeg_path
        max_mb = self._settings.bili_processing_asr_max_file_size_mb
        seg_min = self._settings.bili_processing_audio_max_segment_minutes
        max_input_tokens = self._settings.bili_processing_asr_max_input_tokens
        tokens_per_second = self._settings.bili_processing_asr_tokens_per_second
        asr_language = self._settings.bili_processing_asr_language

        try:
            downloader = AudioDownloader(credential=credential)
            page_results = []
            total_duration: float = 0.0
            total_chars: int = 0

            for page in pages:
                page_index = page["page_index"]
                m4s_path = temp_base / f"{page_index}.m4s"

                # 1. CDN download
                audio_info = await downloader.get_audio_url(
                    bvid, page_index=page_index,
                    quality=self._settings.bili_processing_audio_quality,
                )
                await downloader.download_to_file(
                    audio_info["url"], str(m4s_path), bvid,
                )

                # 2. Convert (+ segment if over token budget or size limit)
                page_duration_raw = page.get("duration")
                page_duration_for_split: float | None
                try:
                    page_duration_for_split = (
                        float(page_duration_raw)
                        if page_duration_raw is not None
                        else None
                    )
                except (TypeError, ValueError):
                    page_duration_for_split = None
                mp3_files = await convert_single(
                    m4s_path, temp_base / f"mp3_{page_index}",
                    max_file_size_mb=max_mb,
                    segment_minutes=seg_min,
                    ffmpeg_setting=ffmpeg_setting,
                    duration_seconds=page_duration_for_split,
                    max_input_tokens=max_input_tokens,
                    tokens_per_second=tokens_per_second,
                )

                # 3. ASR per segment.
                # Sum segment durations rather than overwrite, otherwise
                # ``page_duration`` would end up holding only the last segment's
                # length when the clip is split into multiple pieces.
                texts: list[str] = []
                cdn_duration = audio_info.get("duration")
                segment_duration_sum: float = 0.0
                got_any_segment_duration = False
                for mp3_file in mp3_files:
                    audio_bytes = mp3_file.read_bytes()
                    asr_result = await self._asr_backend.transcribe(
                        audio_bytes, mime_type="audio/mp3",
                        language=asr_language,
                    )
                    texts.append(asr_result.text)
                    if asr_result.duration is not None:
                        segment_duration_sum += float(asr_result.duration)
                        got_any_segment_duration = True

                # Prefer the page metadata's known duration in seconds (the
                # value used to make the segmentation decision); fall back to
                # the summed ASR durations; finally to the CDN-reported value.
                if page_duration_for_split is not None:
                    page_duration: float | None = page_duration_for_split
                elif got_any_segment_duration:
                    page_duration = segment_duration_sum
                else:
                    page_duration = (
                        float(cdn_duration) if cdn_duration is not None else None
                    )

                full_text = " ".join(t for t in texts if t)
                page_results.append({
                    "page_index": page_index,
                    "cid": page.get("cid", 0),
                    "duration": page_duration,
                    "text": full_text,
                    "language": asr_language,
                    "asr_model": getattr(self._asr_backend, "model", ""),
                    "segments": [],
                })
                if page_duration is not None:
                    total_duration += float(page_duration)
                total_chars += len(full_text)

            return {
                "bvid": bvid,
                "pages": page_results,
                "total_duration": total_duration,
                "total_chars": total_chars,
            }

        finally:
            # Always clean up temp files for this bvid.
            shutil.rmtree(str(temp_base), ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Decide whether *exc* is a transient error worth retrying.

        AudioError subclasses (DownloadError, ASRConnectionError, ConvertError,
        ASRAPIError, AudioSizeError) are considered retryable because they
        typically stem from network issues, API timeouts, or temporary
        resource problems.

        All other exceptions (TransformError, RuntimeError, etc.) are treated
        as non-retryable.
        """
        return isinstance(exc, AudioError)

    def _select_pipelines(self, pipelines: list[str] | None) -> list[str]:
        if pipelines is None:
            return [_TRANSFORM, _AUDIO]
        out = []
        for p in pipelines:
            if p in (_TRANSFORM, _AUDIO):
                out.append(p)
        return out or [_TRANSFORM]

    def _select_item_types(self, item_types: list[str] | None) -> list[str]:
        registered = HANDLERS.names()
        if item_types is None:
            return registered
        return [it for it in item_types if it in registered]

    async def _load_or_init_task(
        self, uid: int, pipelines: list[str],
    ) -> ProcessingTaskValue:
        existing = await self._data.get(_task_key(uid))
        if existing is not None:
            return ProcessingTaskValue.from_dict(existing)
        now = int(time.time() * 1000)
        tv = ProcessingTaskValue(
            uid=uid,
            status=ProcessingTaskStatus.PENDING,
            pipelines={p: PipelineEntry() for p in pipelines},
            created_at=now,
            updated_at=now,
        )
        await self._save_task(tv)
        return tv

    async def _reload_task(self, uid: int) -> ProcessingTaskValue | None:
        existing = await self._data.get(_task_key(uid))
        if existing is None:
            return None
        return ProcessingTaskValue.from_dict(existing)

    async def _save_task(self, tv: ProcessingTaskValue) -> None:
        await self._data.put(_task_key(tv.uid), tv.to_dict())

    async def _write_progress(
        self,
        uid: int,
        pipeline: str,
        item_type: str,
        counts: dict[str, int],
        done: bool,
    ) -> None:
        total = counts.get("total", 0)
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        remaining = max(0, total - completed - failed - skipped)
        await self._data.put(_progress_key(uid, pipeline, item_type), {
            "pipeline": pipeline,
            "item_type": item_type,
            "total_items": total,
            "completed_items": completed,
            "failed_items": failed,
            "skipped_items": skipped,
            "remaining_items": remaining,
            "done": done,
        })

    async def _cleanup_temp(self, uid: int) -> None:
        """Remove residual temp files for a uid after processing."""
        temp_uid_dir = Path(self._temp_dir) / str(uid)
        if temp_uid_dir.exists():
            shutil.rmtree(str(temp_uid_dir), ignore_errors=True)
            logger.debug("temp_cleaned", extra={"uid": uid})

    @staticmethod
    def _derive_pipeline_status(
        rollup: dict[str, dict[str, int]],
    ) -> ProcessingPipelineStatus:
        if not rollup:
            return ProcessingPipelineStatus.SUCCESS  # nothing to do == done
        any_failed = False
        any_completed = False
        any_pending = False
        for counts in rollup.values():
            total = counts.get("total", 0)
            failed = counts.get("failed", 0)
            completed = counts.get("completed", 0)
            skipped = counts.get("skipped", 0)
            if failed > 0:
                any_failed = True
            if completed > 0:
                any_completed = True
            if total - completed - failed - skipped > 0:
                any_pending = True
        if any_pending:
            return ProcessingPipelineStatus.RUNNING
        if any_failed and any_completed:
            return ProcessingPipelineStatus.PARTIAL
        if any_failed and not any_completed:
            return ProcessingPipelineStatus.FAILED_PERMANENT
        return ProcessingPipelineStatus.SUCCESS

    @staticmethod
    def _derive_task_status(tv: ProcessingTaskValue) -> ProcessingTaskStatus:
        if not tv.pipelines:
            return ProcessingTaskStatus.SUCCESS
        statuses = [p.status for p in tv.pipelines.values()]
        if all(s == ProcessingPipelineStatus.SUCCESS for s in statuses):
            return ProcessingTaskStatus.SUCCESS
        if any(s == ProcessingPipelineStatus.RUNNING for s in statuses):
            return ProcessingTaskStatus.RUNNING
        if any(s == ProcessingPipelineStatus.FAILED_PERMANENT for s in statuses) and \
           not any(s == ProcessingPipelineStatus.SUCCESS for s in statuses):
            return ProcessingTaskStatus.FAILED_PERMANENT
        if any(s in (ProcessingPipelineStatus.PARTIAL,
                     ProcessingPipelineStatus.FAILED_PERMANENT,
                     ProcessingPipelineStatus.FAILED_RETRYABLE,
                     ProcessingPipelineStatus.PENDING) for s in statuses):
            return ProcessingTaskStatus.PARTIAL
        return ProcessingTaskStatus.SUCCESS
