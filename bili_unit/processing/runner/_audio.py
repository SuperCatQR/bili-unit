# runner._audio — audio pipeline (discover / dispatch / process_audio_one /
# do_audio_work).

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..._types import CredentialProvider
from .. import (
    ProcessingItemStatus,
    ProcessingPipelineStatus,
)
from .._estimate import estimate_audio_work, estimate_exceeds_budget
from ._audio_work import (
    audio_convert_page,
    audio_download_page,
    audio_transcribe_page,
)
from ._pipeline_executor import (
    AudioItemPersistence,
    ItemRetryContext,
    WorkerOutcome,
    WorkItem,
    run_item_with_retry,
    run_item_workers,
)

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from ...parsing._store import ParsingStore
    from .._store import ProcessingStore
    from ..audio._asr_backend import ASRBackend
    from ..audio._asr_cache import ASRCacheStore

# Boundaries (docs/structure/bili.md §8): processing runner must not import
# fetching.auth directly.  The caller (assemble / ProcessingCommand) injects
# a provider callable so this mixin stays decoupled from the fetching stage.
# CredentialProvider itself lives in bili_unit._types. This module imports it
# for type compatibility with existing internal runner construction paths.

logger = logging.getLogger("bili.processing.runner")


_AUDIO = "audio"


class _AudioMixin:
    """Mixin providing audio pipeline methods for :class:`ProcessingRunner`.

    Accesses runner state (``self._store``, ``self._parse_store``,
    ``self._settings``, ``self._temp_dir``,
    ``self._asr_backend``, ``self._credential_provider``) and helpers
    (``_get_asr_cache``, ``_derive_pipeline_status``, ``_is_retryable``)
    via the combined MRO at runtime.
    """

    _store: ProcessingStore | None
    _parse_store: ParsingStore | None
    _settings: BiliSettings
    _temp_dir: Path
    _asr_backend: ASRBackend | None
    _credential_provider: CredentialProvider | None
    _downloader_factory: Any
    _convert_fn: Any

    def _get_asr_cache(self) -> ASRCacheStore | None: ...  # pragma: no cover

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
        mode: str,
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> tuple[list[str], dict, list[str]]:
        """Phase-1 audio: discover → enqueue → workers → rollup.

        Returns the bvid list that survived discovery + filtering (the
        candidate set the worker pool processes, or *would* have processed
        on dry-run). The list mirrors the order items are enqueued.
        """
        store = self._store
        await store.update_task_pipeline(
            _AUDIO, status=ProcessingPipelineStatus.RUNNING.value,
        )

        # 1. discover audio work items (one per bvid)
        try:
            audio_items, skipped, subtitle_done = await self._discover_audio_items(
                uid, mode,
                only_bvids=only_bvids,
                retry_failed_only=retry_failed_only,
                limit=limit,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audio_discovery_failed",
                extra={"uid": uid, "error": str(exc)},
            )
            audio_items, skipped, subtitle_done = [], 0, 0

        candidates = [it.item_id for it in audio_items]
        estimate = estimate_audio_work(
            audio_items,
            tokens_per_second=self._settings.bili_processing_asr_tokens_per_second,
        )
        budget_exceeded = estimate_exceeds_budget(
            estimate,
            max_audio_seconds=max_audio_seconds,
            max_audio_tokens=max_audio_tokens,
        )

        rollup: dict[str, dict[str, int]] = {
            "transcription": {
                "total": len(audio_items) + skipped + subtitle_done,
                "completed": subtitle_done,
                "failed": 0,
                "skipped": skipped,
            },
        }
        await store.update_task_pipeline(
            _AUDIO, status=ProcessingPipelineStatus.RUNNING.value,
            items=rollup,
        )

        if budget_exceeded:
            logger.warning(
                "audio_budget_exceeded",
                extra={
                    "uid": uid,
                    "candidates": len(candidates),
                    "budget_exceeded": budget_exceeded,
                    "estimate": estimate.to_dict(),
                },
            )
            rollup["transcription"]["skipped"] += len(audio_items)
            rollup["transcription"]["total"] = subtitle_done + skipped + len(audio_items)
            final_status = ProcessingPipelineStatus.PARTIAL
            await store.update_task_pipeline(
                _AUDIO, status=final_status.value, items=rollup,
            )
            return candidates, estimate.to_dict(), budget_exceeded

        if dry_run:
            # Skip credential resolution / worker dispatch entirely. We
            # zero the rollup so the pipeline status derives to SUCCESS
            # (nothing to do == done) without lying that anything ran;
            # the truth lives in ``candidates`` which the caller surfaces
            # via ``ProcessingCommandResult.dry_run_candidates``.
            logger.info(
                "audio_dry_run",
                extra={"uid": uid, "candidates": candidates, "skipped": skipped},
            )
            print(f"dry_run candidates: {candidates}")
            rollup["transcription"] = {
                "total": 0, "completed": 0, "failed": 0, "skipped": 0,
            }
            final_status = self._derive_pipeline_status(rollup)
            await store.update_task_pipeline(
                _AUDIO, status=final_status.value, items=rollup,
            )
            return candidates, estimate.to_dict(), []

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
        final_status = self._derive_pipeline_status(rollup)
        await store.update_task_pipeline(
            _AUDIO, status=final_status.value, items=rollup,
        )
        return candidates, estimate.to_dict(), []

    async def _discover_audio_items(
        self: Any,
        uid: int,
        mode: str,
        *,
        only_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> tuple[list[WorkItem], int, int]:
        """Discover audio work items from parsed main-DB video rows.

        Reads ``video`` + ``video_page`` through the parsing store. Each bvid
        produces one :class:`WorkItem` carrying its page list.

        Filter ordering (each step narrows the previous one's output):
            1. ``only_bvids``      — restrict to a caller-supplied bvid set.
            2. ``_filter_audio_ready`` (incremental skip /
               ``retry_failed_only`` selection).
            3. subtitle short-circuit — bvids whose ``video_subtitle`` parsing
               object exists and is complete have their result written
               directly with ``transcription_source: "subtitle"`` and are
               removed from the worker queue.
            4. ``limit``            — cap to first N after the steps above.

        Returns ``(ready_items, skipped_count, subtitle_done_count)``.
        """
        parse_store = self._parse_store
        payloads = await parse_store.list_video_page_work_items()
        if not payloads:
            return [], 0, 0

        only_set: set[str] | None = (
            set(only_bvids) if only_bvids is not None else None
        )

        items: list[WorkItem] = []
        # Sorted iteration → stable discovery order across runs.
        for bvid in sorted(payloads.keys()):
            if only_set is not None and bvid not in only_set:
                continue
            payload = payloads[bvid]
            if not isinstance(payload, list) or not payload:
                continue

            items.append(WorkItem(
                item_type="audio",
                item_id=bvid,
                item_data={"bvid": bvid, "pages": payload},
            ))

        ready, skipped = await self._filter_audio_ready(
            uid, items, mode, retry_failed_only=retry_failed_only,
        )

        ready, subtitle_done = await self._apply_subtitle_shortcuts(
            uid, ready, dry_run=dry_run,
        )

        if limit is not None and limit >= 0:
            ready = ready[:limit]
        return ready, skipped, subtitle_done

    async def _apply_subtitle_shortcuts(
        self: Any,
        uid: int,
        items: list[WorkItem],
        *,
        dry_run: bool,
    ) -> tuple[list[WorkItem], int]:
        """Remove bvids that have complete subtitles and persist their results.

        For each item whose parsed ``video_subtitle`` exists and is complete
        (every page has a selected language), build an audio-pipeline result
        dict from the subtitle segments and write a SUCCESS row to the
        processing store.  Those items are removed from the returned list so
        workers do not re-process them.

        ``dry_run`` skips both the read and the persistence: short-circuit
        candidates would still flow through the worker pool on a real run,
        and we don't want dry-run to mutate processing storage.
        """
        if dry_run or self._parse_store is None or not items:
            return items, 0

        store = self._store
        parse_store = self._parse_store

        remaining: list[WorkItem] = []
        done_count = 0
        for item in items:
            bvid = item.item_id
            try:
                subtitle = await parse_store.get_video_subtitle_payload(bvid)
            except Exception:  # noqa: BLE001 — defensive: never block audio
                logger.warning(
                    "subtitle_lookup_failed",
                    extra={"uid": uid, "bvid": bvid},
                    exc_info=True,
                )
                remaining.append(item)
                continue

            result = self._build_subtitle_result(item, subtitle)
            if result is None:
                remaining.append(item)
                continue

            now = int(time.time() * 1000)
            payload = {
                "uid": uid,
                "pipeline": "audio",
                "item_type": "transcription",
                "item_id": bvid,
                "status": ProcessingItemStatus.SUCCESS.value,
                "result": result,
                "source_endpoints": ["video_subtitle"],
                "processed_at": now,
            }
            transcript = " ".join(
                p.get("text", "") for p in result["pages"] if isinstance(p, dict)
            )
            await store.save_audio_transcription(
                bvid,
                status="success",
                transcription_source=result.get("transcription_source"),
                transcript=transcript or None,
                audio_tokens=0,
                seconds=0,
                cache_hits=0,
                payload=payload,
                processed_at_ms=now,
            )
            done_count += 1
            logger.info(
                "audio_subtitle_shortcut",
                extra={"uid": uid, "bvid": bvid, "pages": len(result["pages"])},
            )
        return remaining, done_count

    @staticmethod
    def _build_subtitle_result(
        item: WorkItem,
        subtitle: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return an audio-result dict synthesised from a subtitle dict.

        Returns None when ``subtitle`` is missing or not complete (any page
        without a resolved language).  The caller falls back to the ASR
        pipeline in that case.
        """
        if not isinstance(subtitle, dict):
            return None
        pages = subtitle.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        expected_page_indexes = {
            int(p["page_index"])
            for p in item.item_data.get("pages", [])
            if isinstance(p, dict) and "page_index" in p
        }
        subtitle_page_indexes: set[int] = set()

        for p in pages:
            if not isinstance(p, dict):
                return None
            page_index = int(p.get("page_index", 0) or 0)
            subtitle_page_indexes.add(page_index)
            lan = str(p.get("lan", "") or "")
            if not lan:
                return None
            # Bilibili AI subtitles are useful reference material, but not
            # reliable enough to replace this project's own ASR pass.
            if bool(p.get("is_ai")) or lan.startswith("ai-"):
                return None
        if expected_page_indexes and subtitle_page_indexes != expected_page_indexes:
            return None

        bvid_pages = {
            p["page_index"]: p for p in item.item_data.get("pages", [])
            if isinstance(p, dict) and "page_index" in p
        }

        out_pages: list[dict[str, Any]] = []
        total_duration = 0.0
        total_chars = 0
        for sp in pages:
            page_index = int(sp.get("page_index", 0) or 0)
            cid = int(sp.get("cid", 0) or 0)
            lan = str(sp.get("lan", "") or "")
            seg_dicts = sp.get("segments", []) or []
            segments_out: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for seg in seg_dicts:
                if not isinstance(seg, dict):
                    continue
                start = float(seg.get("start", 0.0) or 0.0)
                end = float(seg.get("end", 0.0) or 0.0)
                content = str(seg.get("content", "") or "")
                segments_out.append({
                    "start_s": start,
                    "end_s": end,
                    "text": content,
                    "duration": max(0.0, end - start),
                    "model": "subtitle",
                })
                text_parts.append(content)
            text = " ".join(text_parts)

            duration_meta = bvid_pages.get(page_index, {}).get("duration")
            try:
                duration = (
                    float(duration_meta) if duration_meta is not None else None
                )
            except (TypeError, ValueError):
                duration = None
            if duration is None and segments_out:
                duration = segments_out[-1]["end_s"]

            out_pages.append({
                "page_index": page_index,
                "cid": cid,
                "duration": duration,
                "text": text,
                "language": lan,
                "asr_model": "subtitle",
                "segments": segments_out,
            })
            if duration is not None:
                total_duration += float(duration)
            total_chars += len(text)

        return {
            "bvid": item.item_id,
            "pages": out_pages,
            "total_duration": total_duration,
            "total_chars": total_chars,
            "transcription_source": "subtitle",
            "cost": {
                "audio_tokens": 0,
                "seconds": 0,
                "model": "subtitle",
                "cache_hits": 0,
                "fresh_segments": 0,
            },
        }

    async def _filter_audio_ready(
        self: Any,
        uid: int,
        items: list[WorkItem],
        mode: str,
        *,
        retry_failed_only: bool = False,
    ) -> tuple[list[WorkItem], int]:
        """Apply incremental skip rule for audio items.

        ``retry_failed_only`` overrides the normal incremental rule: only
        items whose existing status is FAILED are kept; everything else
        (no record yet, SUCCESS, anything other than FAILED) is dropped
        without being counted as ``skipped`` so the rollup reflects
        "considered for retry" rather than the full population.
        """
        store = self._store
        if retry_failed_only:
            ready: list[WorkItem] = []
            for item in items:
                status = await store.get_audio_status(item.item_id)
                if status == "failed":
                    ready.append(item)
            return ready, 0

        if mode == "full":
            return items, 0
        ready = []
        skipped = 0
        for item in items:
            status = await store.get_audio_status(item.item_id)
            if status is None:
                ready.append(item)
                continue
            if status == "success":
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
            retry_event="audio_item_retry",
            failed_event="audio_item_failed",
            log_id_field="bvid",
            log_id_value=bvid,
        )
        return await run_item_with_retry(
            ctx,
            store=self._store,
            persistence=AudioItemPersistence(),
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

        temp_base = self._temp_dir / str(uid) / "audio" / bvid
        temp_base.mkdir(parents=True, exist_ok=True)

        asr_language = self._settings.bili_processing_asr_language

        try:
            downloader = self._downloader_factory(credential=credential)
            page_results = []
            total_duration: float = 0.0
            total_chars: int = 0
            total_audio_tokens: int = 0
            total_asr_seconds: float = 0.0
            total_cache_hits: int = 0
            total_fresh_segments: int = 0

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
                    high_risk_split_enabled=(
                        self._settings
                        .bili_processing_asr_high_risk_split_enabled
                    ),
                    high_risk_split_seconds=(
                        self._settings.bili_processing_asr_high_risk_split_seconds
                    ),
                    high_risk_min_segment_seconds=(
                        self._settings
                        .bili_processing_asr_high_risk_min_segment_seconds
                    ),
                    empty_segment_skip_seconds=(
                        self._settings.bili_processing_asr_empty_segment_skip_seconds
                    ),
                    ffmpeg_setting=self._settings.bili_processing_ffmpeg_path,
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
                    "segments": trans["segments"],
                })
                if page_duration is not None:
                    total_duration += float(page_duration)
                total_chars += len(full_text)
                total_audio_tokens += int(trans.get("audio_tokens_total", 0))
                total_asr_seconds += float(trans.get("asr_seconds_total", 0.0))
                total_cache_hits += int(trans.get("cache_hits", 0))
                total_fresh_segments += int(trans.get("fresh_segment_count", 0))

            result = {
                "bvid": bvid,
                "pages": page_results,
                "total_duration": total_duration,
                "total_chars": total_chars,
                "transcription_source": "asr",
                "cost": {
                    "audio_tokens": int(total_audio_tokens),
                    "seconds": int(total_asr_seconds),
                    "model": getattr(self._asr_backend, "model", ""),
                    "cache_hits": int(total_cache_hits),
                    "fresh_segments": int(total_fresh_segments),
                },
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
