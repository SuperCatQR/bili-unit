# runner._audio — audio pipeline (discover / dispatch / process_audio_one /
# do_audio_work).

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..._logging import Progress
from ..._retry import (
    RetryClassification,
    RetryDriver,
    RetryOutcome,
    RetryPolicy,
)
from .. import (
    ProcessingItemStatus,
    ProcessingPipelineStatus,
)
from ..keys import _proc_key, _task_key
from ..task import PipelineEntry, ProcessingTaskValue
from ..transform._base import WorkItem
from ._audio_work import (
    audio_convert_page,
    audio_download_page,
    audio_transcribe_page,
)

if TYPE_CHECKING:
    from ..audio._asr_backend import ASRBackend
    from ..audio._asr_cache import ASRCacheStore
    from ..env import ProcessingEnv

logger = logging.getLogger("bili.processing.runner")


_AUDIO = "audio"


class _AudioMixin:
    """Mixin providing audio pipeline methods for :class:`ProcessingRunner`.

    Accesses runner state (``self._data``, ``self._error``, ``self._fetch_qry``,
    ``self._settings``, ``self._temp_dir``, ``self._asr_backend``) and helpers
    (``_get_asr_cache``, ``_write_progress``, ``_derive_pipeline_status``,
    ``_is_retryable``) via the combined MRO at runtime.
    """

    _data: Any
    _error: Any
    _fetch_qry: Any
    _settings: ProcessingEnv
    _temp_dir: str
    _asr_backend: ASRBackend | None

    def _get_asr_cache(self) -> ASRCacheStore | None: ...  # pragma: no cover

    async def _write_progress(
        self, uid: int, pipeline: str, item_type: str,
        counts: dict[str, int], done: bool,
    ) -> None: ...  # pragma: no cover

    @staticmethod
    def _derive_pipeline_status(
        rollup: dict[str, dict[str, int]],
    ) -> ProcessingPipelineStatus: ...  # pragma: no cover

    @staticmethod
    def _is_retryable(exc: Exception) -> bool: ...  # pragma: no cover

    # -- audio pipeline ----------------------------------------------------

    async def _run_audio(
        self: Any,
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
                from ...fetching.auth import get_credential
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
        self: Any,
        uid: int,
        mode: str,
    ) -> tuple[list[WorkItem], int]:
        """Discover audio work items from video_detail.

        Each bvid produces one WorkItem carrying its page list.
        Returns (ready_items, skipped_count).
        """
        from ...fetching import EndpointStatus as _EpStatus

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
        self: Any,
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
        self: Any,
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
        bar = Progress(total=len(items), label=f"audio uid={uid}")

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
                bar.update(
                    1,
                    postfix=f"bvid={item.item_id} {'ok' if ok else 'fail'}",
                )

        try:
            await asyncio.gather(
                producer(),
                *[worker(i) for i in range(worker_count)],
            )
        finally:
            bar.close()

    async def _process_audio_one(
        self: Any,
        uid: int,
        item: WorkItem,
        credential: Any,
    ) -> bool:
        """Process a single bvid through the audio pipeline with retry.

        Drives ``_do_audio_work`` through :class:`RetryDriver`; ``_is_retryable``
        decides whether a given exception is transient.
        """
        bvid = item.item_id
        key = _proc_key(uid, "audio", bvid)
        max_retries = self._settings.bili_processing_max_retries
        retry_delays = self._settings.get_retry_delays()

        async def _do_work():
            return await self._do_audio_work(uid, item, credential)

        def _classify(exc: Exception) -> RetryClassification:
            return (
                RetryClassification.RETRYABLE
                if self._is_retryable(exc)
                else RetryClassification.PERMANENT
            )

        async def _on_attempt_failed(
            exc: Exception, outcome: RetryOutcome,
        ) -> int | None:
            now = int(time.time() * 1000)
            if outcome.will_retry:
                logger.info(
                    "audio_item_retry",
                    extra={"uid": uid, "bvid": bvid,
                           "retry": outcome.attempt, "delay_s": outcome.delay_seconds,
                           "error": str(exc)},
                )
                await self._error.record(
                    exc, uid=uid, pipeline=_AUDIO,
                    item_type="transcription", item_id=bvid,
                    retryable="true",
                    detail={"retry_count": outcome.attempt},
                )
                await self._data.put(key, {
                    "uid": uid, "pipeline": _AUDIO,
                    "item_type": "transcription", "item_id": bvid,
                    "status": ProcessingItemStatus.FAILED.value,
                    "result": None,
                    "source_endpoints": ["video_detail"],
                    "processed_at": now,
                    "retry_count": outcome.attempt,
                })
                return None

            # Final failure (PERMANENT or exhausted retries).
            logger.warning(
                "audio_item_failed",
                extra={"uid": uid, "bvid": bvid,
                       "retry_count": outcome.attempt, "error": str(exc)},
            )
            await self._error.record(
                exc, uid=uid, pipeline=_AUDIO,
                item_type="transcription", item_id=bvid,
                retryable="false",
                detail=(
                    {"retry_count": outcome.attempt}
                    if outcome.attempt > 1 else None
                ),
            )
            await self._data.put(key, {
                "uid": uid, "pipeline": _AUDIO,
                "item_type": "transcription", "item_id": bvid,
                "status": ProcessingItemStatus.FAILED.value,
                "result": None,
                "source_endpoints": ["video_detail"],
                "processed_at": now,
                "retry_count": outcome.attempt if outcome.attempt > 1 else 0,
            })
            return None

        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            delays=retry_delays,
            classify=_classify,
        )
        driver = RetryDriver(policy)

        try:
            result = await driver.run(
                _do_work, on_attempt_failed=_on_attempt_failed,
            )
        except Exception:  # noqa: BLE001 — final state already recorded
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

    async def _do_audio_work(
        self: Any,
        uid: int,
        item: WorkItem,
        credential: Any,
    ) -> dict[str, Any]:
        """Process all pages for a single bvid.  Returns result dict.

        Orchestrates ``audio_download_page`` → ``audio_convert_page`` →
        ``audio_transcribe_page`` for each page.  Temp cleanup and per-bvid
        cache invalidation (on success) live here, not in the helpers.

        Raises on any error; temp files are always cleaned up in ``finally``.
        """
        # Late import: tests patch ``bili_unit.processing.runner.AudioDownloader``
        # and we want each call to honour the current binding.
        from . import AudioDownloader

        bvid = item.item_id
        pages = item.item_data.get("pages", [])

        temp_base = Path(self._temp_dir) / str(uid) / "audio" / bvid
        temp_base.mkdir(parents=True, exist_ok=True)

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
                audio_info = await audio_download_page(
                    downloader, bvid, page_index,
                    quality=self._settings.bili_processing_audio_quality,
                    m4s_path=m4s_path,
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
                mp3_files = await audio_convert_page(
                    m4s_path, temp_base / f"mp3_{page_index}",
                    page_duration_for_split, self._settings,
                )

                # 3. ASR per segment — with resume cache.
                asr_cache = self._get_asr_cache()
                cdn_duration = audio_info.get("duration")
                trans = await audio_transcribe_page(
                    self._asr_backend, asr_cache,
                    uid, bvid, page_index, mp3_files, asr_language,
                )

                segment_duration_sum = trans["segment_duration_sum"]
                got_any_segment_duration = trans["got_any_segment_duration"]
                full_text = trans["text"]

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

            result = {
                "bvid": bvid,
                "pages": page_results,
                "total_duration": total_duration,
                "total_chars": total_chars,
            }
            # Bvid completed successfully — drop its resume cache.  We only
            # clear on the success path; partial-failure cache survives so
            # the next retry can resume cheaply.
            asr_cache_for_clear = self._get_asr_cache()
            if asr_cache_for_clear is not None:
                asr_cache_for_clear.clear_bvid(uid, bvid)
            return result

        finally:
            # Always clean up temp files for this bvid.
            shutil.rmtree(str(temp_base), ignore_errors=True)
