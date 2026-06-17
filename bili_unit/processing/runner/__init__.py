# runner — orchestrates the processing task for a uid (audio pipeline only).
#
#   Phase 0  扫描     读取 fetching task；生成工作项；增量/全量决策
#   Phase 1  分发执行 入队 audio_queue；启动 worker pool；处理
#   Phase 2  收尾     汇总状态；更新 processing task；清理 temp
#
# Concurrency control: asyncio.Queue with maxsize from
# BILI_PROCESSING_QUEUE_MAXSIZE. Workers exit on sentinel (None).
#
# Phase 3.3: ``ProcessingStore`` (SQLite) replaces the old
# ProcessingDataStore + ProcessingErrorStore pair. The runner is
# constructed once with cross-uid services (settings / asr_backend /
# downloader / convert_fn) and the per-uid stores enter via :meth:`run`.
# The audio mixin reads them back from ``self`` for the duration of one
# call.

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import (
    AudioError,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from ..audio._asr_cache import ASRCacheStore
from ..audio._converter import convert_single as _real_convert_single
from ..audio._downloader import AudioDownloader as _real_audio_downloader_cls
from ._audio import CredentialProvider, _AudioMixin
from ._audio_work import (
    ConvertFn,
    audio_convert_page,
    audio_download_page,
    audio_transcribe_page,
)

DownloaderFactory = Callable[..., Any]  # AudioDownloader constructor, compatible with AudioDownloader.__init__

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from ...parsing._store import ParsingStore
    from .._store import ProcessingStore
    from ..audio._asr_backend import ASRBackend

logger = logging.getLogger("bili.processing.runner")


_AUDIO = "audio"


class ProcessingRunner(_AudioMixin):
    """Orchestrate the audio pipeline for a uid."""

    def __init__(
        self,
        settings: BiliSettings,
        *,
        asr_backend: ASRBackend | None = None,
        credential_provider: CredentialProvider | None = None,
        downloader_factory: DownloaderFactory | None = None,
        convert_fn: ConvertFn | None = None,
    ) -> None:
        self._settings = settings
        self._asr_backend = asr_backend
        self._credential_provider = credential_provider
        self._downloader_factory = (
            downloader_factory if downloader_factory is not None else _real_audio_downloader_cls
        )
        self._convert_fn = (
            convert_fn if convert_fn is not None else _real_convert_single
        )
        self._asr_cache: ASRCacheStore | None = None
        # Per-uid stores assigned for the duration of ``run``.
        self._store: ProcessingStore | None = None
        self._parse_store: ParsingStore | None = None

    # ``_temp_dir`` is derived from settings on demand (per-uid scoping is
    # done inside _do_audio_work). Exposed as a property so audio code can
    # keep using ``self._temp_dir / str(uid) / "audio" / bvid`` unchanged.
    @property
    def _temp_dir(self) -> Path:
        return Path(self._settings.bili_processing_temp_dir)

    def _get_asr_cache(self) -> ASRCacheStore | None:
        """Lazy-build the ASR resume cache when enabled.

        Returns None when the cache is disabled — callers should treat that
        as "no caching" and proceed straight to the backend.
        """
        if not self._settings.bili_processing_asr_cache_enabled:
            return None
        if self._asr_cache is None:
            self._asr_cache = ASRCacheStore(
                self._settings.bili_processing_asr_cache_dir,
            )
        return self._asr_cache

    # -- public API --------------------------------------------------------

    async def run(
        self,
        uid: int,
        *,
        proc_store: ProcessingStore,
        parse_store: ParsingStore,
        mode: str = "incremental",
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        exclude_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> tuple[ProcessingTaskStatus, list[str], dict | None, list[str], dict | None]:
        """Run the audio processing pipeline for a uid.

        Args:
            uid: target user.
            proc_store: per-uid processing store (writes audio_transcription
                + stage_task[stage='processing'] + stage_error rows).
            parse_store: per-uid parsing store (read-only here — video pages
                and subtitle payloads from main DB).
            mode: "incremental" (default) or "full".
            limit / only_bvids / exclude_bvids / retry_failed_only / dry_run: see
                :meth:`ProcessingCommand.process_uid`.
            max_audio_seconds / max_audio_tokens: optional pre-dispatch budget
                caps. When exceeded, the runner writes a PARTIAL task state
                and returns without dispatching workers.

        Returns:
            ``(status, candidates, estimate, budget_exceeded, coverage)``.
            ``candidates`` is the bvid list that entered (or *would have
            entered*) the audio worker after all CLI-level filters.
        """
        if mode not in ("incremental", "full"):
            raise ValueError(f"unknown mode: {mode!r}")

        logger.info(
            "processing_start",
            extra={
                "uid": uid, "mode": mode, "pipelines": [_AUDIO],
                "limit": limit, "only_bvids": only_bvids,
                "exclude_bvids": exclude_bvids,
                "retry_failed_only": retry_failed_only, "dry_run": dry_run,
                "max_audio_seconds": max_audio_seconds,
                "max_audio_tokens": max_audio_tokens,
            },
        )

        # Bind per-uid stores so the audio mixin can reach them through ``self``.
        self._store = proc_store
        self._parse_store = parse_store
        try:
            # Phase 0 — seed (or merge) the processing task envelope.
            await proc_store.init_task([_AUDIO])
            await proc_store.update_task_status(
                ProcessingTaskStatus.RUNNING.value,
            )

            # Phase 1 — audio pipeline
            candidates, estimate, budget_exceeded = await self._run_audio(
                uid, mode,
                limit=limit,
                only_bvids=only_bvids,
                exclude_bvids=exclude_bvids,
                retry_failed_only=retry_failed_only,
                dry_run=dry_run,
                max_audio_seconds=max_audio_seconds,
                max_audio_tokens=max_audio_tokens,
            )
            coverage = None
            if self._should_audit_audio_coverage(
                limit=limit,
                only_bvids=only_bvids,
                exclude_bvids=exclude_bvids,
                retry_failed_only=retry_failed_only,
                dry_run=dry_run,
                budget_exceeded=budget_exceeded,
            ):
                coverage = await self._audit_audio_coverage(proc_store, parse_store)

            # Phase 2 — finalise: derive task status from current pipeline rollup.
            task = await proc_store.get_task() or {}
            payload = task.get("payload") or {}
            pipelines = payload.get("pipelines") or {}
            task_status = self._derive_task_status(pipelines)
            if (
                coverage is not None
                and not coverage.get("complete", True)
                and task_status == ProcessingTaskStatus.SUCCESS
            ):
                task_status = ProcessingTaskStatus.PARTIAL
            await proc_store.update_task_status(task_status.value)

            # Phase 2 — cleanup temp
            await self._cleanup_temp(uid)

            logger.info(
                "processing_completed",
                extra={"uid": uid, "status": task_status.value, "coverage": coverage},
            )
            return task_status, candidates, estimate, budget_exceeded, coverage
        finally:
            self._store = None
            self._parse_store = None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Decide whether *exc* is a transient error worth retrying.

        AudioError subclasses (DownloadError, ASRConnectionError, ConvertError,
        ASRAPIError, AudioSizeError) are considered retryable because they
        typically stem from network issues, API timeouts, or temporary
        resource problems.

        ASRConfigError is the deliberate exception: it signals user-side
        misconfiguration (missing key, unknown profile, custom profile without
        base_url). Retrying cannot fix that — fail fast and surface the message.

        All other exceptions (RuntimeError, etc.) are treated as non-retryable.
        """
        from .. import ASRConfigError

        if isinstance(exc, ASRConfigError):
            return False
        return isinstance(exc, AudioError)

    async def _cleanup_temp(self, uid: int) -> None:
        """Remove residual temp files for a uid after processing."""
        temp_uid_dir = self._temp_dir / str(uid)
        if temp_uid_dir.exists():
            shutil.rmtree(str(temp_uid_dir), ignore_errors=True)
            logger.debug("temp_cleaned", extra={"uid": uid})

    @staticmethod
    def _should_audit_audio_coverage(
        *,
        limit: int | None,
        only_bvids: list[str] | None,
        exclude_bvids: list[str] | None,
        retry_failed_only: bool,
        dry_run: bool,
        budget_exceeded: list[str],
    ) -> bool:
        """Return True when the result should report uid-level ASR coverage."""
        _ = retry_failed_only
        return (
            limit is None
            and only_bvids is None
            and exclude_bvids is None
            and not dry_run
            and not budget_exceeded
        )

    async def _audit_audio_coverage(
        self,
        proc_store: ProcessingStore,
        parse_store: ParsingStore,
    ) -> dict[str, Any]:
        """Compare parsed videos with current successful audio rows."""
        page_items = await parse_store.list_video_page_work_items()
        expected = sorted(page_items.keys())
        statuses = await proc_store.list_audio_statuses()
        missing = [bvid for bvid in expected if bvid not in statuses]
        failed = [bvid for bvid in expected if statuses.get(bvid) == "failed"]
        success = sum(1 for bvid in expected if statuses.get(bvid) == "success")
        coverage = {
            "expected": len(expected),
            "success": success,
            "missing": len(missing),
            "failed": len(failed),
            "complete": not missing and not failed,
            "missing_bvids": missing,
            "failed_bvids": failed,
        }
        task = await proc_store.get_task() or {}
        payload = task.get("payload") or {}
        pipelines = payload.get("pipelines") or {}
        audio_entry = pipelines.get(_AUDIO) or {}
        items = audio_entry.get("items") or {}
        current_status = audio_entry.get("status")
        pipeline_status = ProcessingPipelineStatus.SUCCESS
        if current_status == ProcessingPipelineStatus.FAILED_PERMANENT.value:
            pipeline_status = ProcessingPipelineStatus.FAILED_PERMANENT
        elif current_status == ProcessingPipelineStatus.PARTIAL.value:
            pipeline_status = ProcessingPipelineStatus.PARTIAL
        elif not coverage["complete"]:
            pipeline_status = ProcessingPipelineStatus.PARTIAL
        await proc_store.update_task_pipeline(
            _AUDIO,
            status=pipeline_status.value,
            items=items,
            coverage=coverage,
        )
        if not coverage["complete"]:
            logger.warning(
                "audio_coverage_incomplete",
                extra={
                    "expected": coverage["expected"],
                    "success": success,
                    "missing": len(missing),
                    "failed": len(failed),
                    "missing_bvids": missing,
                    "failed_bvids": failed,
                },
            )
        return coverage

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
    def _derive_task_status(
        pipelines: dict[str, dict[str, Any]],
    ) -> ProcessingTaskStatus:
        if not pipelines:
            return ProcessingTaskStatus.SUCCESS
        statuses: list[ProcessingPipelineStatus] = []
        for entry in pipelines.values():
            try:
                statuses.append(
                    ProcessingPipelineStatus(entry.get("status", "PENDING")),
                )
            except ValueError:
                statuses.append(ProcessingPipelineStatus.PENDING)
        if all(s == ProcessingPipelineStatus.SUCCESS for s in statuses):
            return ProcessingTaskStatus.SUCCESS
        if any(s == ProcessingPipelineStatus.RUNNING for s in statuses):
            return ProcessingTaskStatus.RUNNING
        if any(s == ProcessingPipelineStatus.FAILED_PERMANENT for s in statuses) and \
           not any(s == ProcessingPipelineStatus.SUCCESS for s in statuses):
            return ProcessingTaskStatus.FAILED_PERMANENT
        if any(s in (ProcessingPipelineStatus.PARTIAL,
                     ProcessingPipelineStatus.FAILED_PERMANENT,
                     ProcessingPipelineStatus.PENDING) for s in statuses):
            return ProcessingTaskStatus.PARTIAL
        return ProcessingTaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# Re-exports — keep external callers / future code paths importable as
# ``bili_unit.processing.runner.audio_download_page`` etc.
# ---------------------------------------------------------------------------

__all__ = [
    "ConvertFn",
    "DownloaderFactory",
    "ProcessingRunner",
    "audio_convert_page",
    "audio_download_page",
    "audio_transcribe_page",
]
