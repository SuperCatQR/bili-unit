# runner._audio — audio pipeline (discover / dispatch / process_audio_one /
# do_audio_work).

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import (
    ProcessingItemStatus,
    ProcessingPipelineStatus,
)
from ..keys import _proc_key, _task_key
from ...fetching import EndpointStatus
from ..task import PipelineEntry, ProcessingTaskValue
from ._audio_work import (
    audio_convert_page,
    audio_download_page,
    audio_transcribe_page,
)
from ._pipeline_executor import (
    ItemRetryContext,
    WorkerOutcome,
    WorkItem,
    run_item_with_retry,
    run_item_workers,
)

if TYPE_CHECKING:
    from bilibili_api import Credential
    from ..audio._asr_backend import ASRBackend
    from ..audio._asr_cache import ASRCacheStore
    from ..._env import BiliSettings

# Boundaries (docs/structure/bili.md §8): processing runner must not import
# fetching.auth directly.  The caller (assemble / ProcessingCommand) injects
# a provider callable so this mixin stays decoupled from the fetching stage.
CredentialProvider = Callable[[], Awaitable["Credential | None"]]

logger = logging.getLogger("bili.processing.runner")


_AUDIO = "audio"


class _AudioMixin:
    """Mixin providing audio pipeline methods for :class:`ProcessingRunner`.

    Accesses runner state (``self._data``, ``self._error``, ``self._fetch_qry``,
    ``self._settings``, ``self._temp_dir``, ``self._asr_backend``,
    ``self._credential_provider``) and helpers (``_get_asr_cache``,
    ``_write_progress``, ``_derive_pipeline_status``, ``_is_retryable``) via
    the combined MRO at runtime.
    """

    _data: Any
    _error: Any
    _fetch_qry: Any
    _settings: BiliSettings
    _temp_dir: str
    _asr_backend: ASRBackend | None
    _credential_provider: CredentialProvider | None
    _downloader_factory: Any
    _convert_fn: Any

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
        if audio_items and self._credential_provider is not None:
            try:
                credential = await self._credential_provider()
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
        vd_pairs = await self._fetch_qry.list_video_details(uid)
        if not vd_pairs:
            return [], 0

        items: list[WorkItem] = []
        for bvid, status in vd_pairs:
            if status != EndpointStatus.SUCCESS:
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

        async def process_item(item: WorkItem) -> WorkerOutcome:
            try:
                ok = await self._process_audio_one(uid, item, credential)
            except Exception as exc:  # noqa: BLE001 — safety net
                logger.error(
                    "audio_worker_unexpected_error",
                    extra={"uid": uid, "bvid": item.item_id,
                           "error": str(exc)},
                )
                ok = False
            return WorkerOutcome(
                bucket="transcription",
                completed=1 if ok else 0,
                failed=0 if ok else 1,
                postfix=f"bvid={item.item_id} {'ok' if ok else 'fail'}",
            )

        await run_item_workers(
            items=items,
            worker_count=worker_count,
            queue_maxsize=int(self._settings.bili_processing_queue_maxsize),
            label=f"audio uid={uid}",
            rollup=rollup,
            process_item=process_item,
        )

    async def _process_audio_one(
        self: Any,
        uid: int,
        item: WorkItem,
        credential: Any,
    ) -> bool:
        """Process a single bvid through the audio pipeline with retry.

        Delegates retry / error-recording / status-persistence to the shared
        ``run_item_with_retry`` seam; ``_is_retryable`` decides whether a given
        exception is transient.  Only the do-work body (``_do_audio_work``) and
        the audio-specific record identity / log events live here.
        """
        bvid = item.item_id

        async def _do_work():
            return await self._do_audio_work(uid, item, credential)

        ctx = ItemRetryContext(
            uid=uid,
            pipeline=_AUDIO,
            item_type="transcription",
            item_id=bvid,
            source_endpoints=("video_detail",),
            key=_proc_key(uid, "audio", bvid),
            retry_event="audio_item_retry",
            failed_event="audio_item_failed",
            log_id_field="bvid",
            log_id_value=bvid,
        )
        return await run_item_with_retry(
            ctx,
            data=self._data,
            error=self._error,
            do_work=_do_work,
            is_retryable=self._is_retryable,
            max_attempts=self._settings.bili_processing_max_retries + 1,
            delays=self._settings.get_processing_retry_delays(),
            logger=logger,
        )

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
        bvid = item.item_id
        pages = item.item_data.get("pages", [])

        temp_base = Path(self._temp_dir) / str(uid) / "audio" / bvid
        temp_base.mkdir(parents=True, exist_ok=True)

        asr_language = self._settings.bili_processing_asr_language

        try:
            downloader = self._downloader_factory(credential=credential)
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
                    convert_fn=self._convert_fn,
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
