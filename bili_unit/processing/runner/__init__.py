# runner — orchestrates the processing task for a uid (audio pipeline only).
#
# Per docs/design/processing.md §10:
#   Phase 0  扫描     读取 fetching task；生成工作项；增量/全量决策
#   Phase 1  分发执行 入队 audio_queue；启动 worker pool；处理
#   Phase 2  收尾     汇总状态；更新 processing task；清理 temp
#
# Concurrency control: asyncio.Queue with maxsize from
# BILI_PROCESSING_QUEUE_MAXSIZE. Workers exit on sentinel (None).
#
# Split into sub-modules for maintainability:
#   _audio.py      — _AudioMixin (discover / dispatch / process_audio_one / do_audio_work)
#   _audio_work.py — pure per-page helpers (download / convert / transcribe)
#   _pipeline_executor.py — shared queue / worker / rollup mechanics
#
# This module retains: orchestration (run), helpers, and the runner class.
#
# Tests patch ``bili_unit.processing.runner.AudioDownloader``,
# ``bili_unit.processing.runner.convert_single``, and
# ``bili_unit.processing.runner.asyncio``.  Those symbols must therefore
# remain reachable at this package's namespace — see the explicit re-exports
# below.

from __future__ import annotations

import asyncio  # noqa: F401 — re-exported so tests can patch asyncio.sleep
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import (
    AudioError,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)
from ..audio._asr_cache import ASRCacheStore
from ..audio._converter import convert_single  # noqa: F401 — patch target
from ..audio._downloader import AudioDownloader  # noqa: F401 — patch target
from ..keys import _progress_key, _task_key
from ..task import PipelineEntry, ProcessingTaskValue
from ._audio import _AudioMixin
from ._audio_work import (
    audio_convert_page,
    audio_download_page,
    audio_transcribe_page,
)

if TYPE_CHECKING:
    from ...fetching.query import Query as FetchingQuery
    from ..audio._asr_backend import ASRBackend
    from ..data import ProcessingDataStore
    from ..env import ProcessingEnv
    from ..error import ProcessingErrorStore

logger = logging.getLogger("bili.processing.runner")


_AUDIO = "audio"


class ProcessingRunner(_AudioMixin):
    """Orchestrate the audio pipeline for a uid."""

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
        self._asr_cache: ASRCacheStore | None = None

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
        mode: str = "incremental",
    ) -> ProcessingTaskStatus:
        """Run the audio processing pipeline for a uid.

        Args:
            uid: target user.
            mode: "incremental" (default) or "full".
        """
        if mode not in ("incremental", "full"):
            raise ValueError(f"unknown mode: {mode!r}")

        logger.info(
            "processing_start",
            extra={"uid": uid, "mode": mode, "pipelines": [_AUDIO]},
        )

        # Phase 0 — load / create task value
        tv = await self._load_or_init_task(uid)
        tv.status = ProcessingTaskStatus.RUNNING
        await self._save_task(tv)

        # Phase 1 — audio pipeline
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

    async def _load_or_init_task(
        self, uid: int,
    ) -> ProcessingTaskValue:
        existing = await self._data.get(_task_key(uid))
        if existing is not None:
            return ProcessingTaskValue.from_dict(existing)
        now = int(time.time() * 1000)
        tv = ProcessingTaskValue(
            uid=uid,
            status=ProcessingTaskStatus.PENDING,
            pipelines={_AUDIO: PipelineEntry()},
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


# ---------------------------------------------------------------------------
# Re-exports — keep external callers / future code paths importable as
# ``bili_unit.processing.runner.audio_download_page`` etc.
# ---------------------------------------------------------------------------

__all__ = [
    "AudioDownloader",
    "ProcessingRunner",
    "audio_convert_page",
    "audio_download_page",
    "audio_transcribe_page",
    "convert_single",
]

_ = Any  # pragma: no cover
