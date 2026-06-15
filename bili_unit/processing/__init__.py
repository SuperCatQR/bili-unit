# bili_unit/processing — common DTOs, exceptions.
#
# Phase 3.3: ``ProcessingStore`` (SQLite) replaces the old file-directory
# ``ProcessingDataStore`` + ``ProcessingErrorStore`` pair.  ``assemble()``
# now returns a single ``ProcessingCommand``; per-uid stores are constructed
# inside ``ProcessingCommand.process_uid``.
#
# DTOs (``ProcessingItemDTO`` / ``ProcessingTaskDTO`` / ...) and the legacy
# ``ProcessingErrorDTO`` are still imported by tests and the legacy
# ``ProcessingQuery``; Phase 4 prunes them. They are kept here as inert
# shape definitions.

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class ProcessingTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_EXHAUSTED = "FAILED_EXHAUSTED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class ProcessingItemStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ProcessingPipelineStatus(StrEnum):
    """Status of a single pipeline (audio) within a task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProcessingError(Exception):
    """Base for all processing-layer exceptions."""


class AudioError(ProcessingError):
    """audio 阶段错误基类。"""


class ASRConfigError(AudioError):
    """ASR 后端配置错误（缺 key、profile 不识别、custom 缺 base_url 等）。

    与 ASRConnectionError / ASRAPIError 区分：本类是用户配置问题，重试无意义；
    runner._is_retryable 把它视作非 retryable（继承自 AudioError 但语义层面
    属于"立刻报错并提示如何修"）。"""


class DownloadError(AudioError):
    """CDN 下载失败。"""


class ConvertError(AudioError):
    """音频格式转换失败（ffmpeg）。"""


class ASRConnectionError(AudioError):
    """ASR API 连接失败。"""


class ASRAPIError(AudioError):
    """ASR API 返回错误。"""


class AudioSizeError(AudioError):
    """音频超出大小限制。"""


class QueueError(ProcessingError):
    """队列操作错误。"""


class DataError(ProcessingError):
    """存储 / 序列化失败。

    The legacy file-directory stores (``data.py`` / ``error.py``, still on
    disk for old tests) need a multi-inheritance hybrid with
    :class:`bili_unit._storage.DecodeError`. To keep the import graph clean
    we register that hybrid lazily; this base class stays a plain
    ``ProcessingError`` subclass.
    """


# ---------------------------------------------------------------------------
# DTOs (Phase 4 will prune these; kept here for ProcessingQuery compatibility)
# ---------------------------------------------------------------------------

@dataclass
class ProcessingErrorDTO:
    """Read-only error record returned by ProcessingQuery.list_errors()."""

    id: int
    uid: int | None
    pipeline: str | None
    item_type: str | None
    item_id: str | None
    error_type: str
    message: str
    retryable: bool | None  # True / False / None when unknown (legacy "unknown")
    detail: dict[str, Any] | None = None
    timestamp: int | None = None


@dataclass
class ProcessingItemDTO:
    uid: int
    pipeline: str
    item_type: str
    item_id: str
    status: ProcessingItemStatus
    result: dict[str, Any] | None = None
    processed_at: int | None = None
    errors: list[ProcessingErrorDTO] = field(default_factory=list)


@dataclass
class ProcessingPipelineDTO:
    name: str
    status: ProcessingPipelineStatus
    items: dict[str, dict[str, int]] = field(default_factory=dict)
    """items[item_type] → {total, completed, failed, skipped}."""


@dataclass
class ProcessingTaskDTO:
    uid: int
    status: ProcessingTaskStatus
    pipelines: dict[str, ProcessingPipelineDTO] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None
    failed_item_ids: list[str] = field(default_factory=list)
    """Aggregated identifiers of failed work units; entries encoded as
    ``"pipeline:item_type:item_id"`` (e.g. ``"audio:transcription:BV1abc"``)."""


@dataclass
class ProcessingCommandResult:
    uid: int
    status: ProcessingTaskStatus
    dry_run_candidates: list[str] | None = None
    """When dry_run was requested, the bvid list that *would* have been
    dispatched to the audio pipeline. ``None`` outside dry-run mode."""


# ---------------------------------------------------------------------------
# Assembly root — picks ASR backend, returns a single ProcessingCommand
# ---------------------------------------------------------------------------

async def assemble(
    settings,
    *,
    asr_backend_override: str | None = None,
    credential_provider=None,
):
    """Return a configured :class:`ProcessingCommand`.

    Args:
        settings: ``BiliSettings`` already loaded by the caller.
        asr_backend_override: takes precedence over BILI_PROCESSING_ASR_BACKEND.
        credential_provider: async callable returning a ``Credential | None``;
            defaults to ``bili_unit.fetching.auth.get_credential`` when None.

    Per Phase 3 conventions, the returned command does NOT pre-open any
    per-uid stores; each ``process_uid`` call constructs its own
    :class:`UidContext` + stores and tears them down on return. The caller
    only needs to call :meth:`ProcessingCommand.close` to release the
    ASR backend's HTTP session (if any).
    """
    from ..fetching.auth import get_credential
    from .audio._asr_backend import create_asr_backend
    from .command import ProcessingCommand

    backend_name = asr_backend_override or settings.bili_processing_asr_backend
    asr_backend = create_asr_backend(backend_name, settings=settings)

    if credential_provider is None:
        credential_provider = get_credential

    return ProcessingCommand(
        settings,
        asr_backend=asr_backend,
        credential_provider=credential_provider,
    )


__all__ = [
    "ASRAPIError",
    "ASRConfigError",
    "ASRConnectionError",
    "AudioError",
    "AudioSizeError",
    "ConvertError",
    "DataError",
    "DownloadError",
    "ProcessingCommandResult",
    "ProcessingError",
    "ProcessingErrorDTO",
    "ProcessingItemDTO",
    "ProcessingItemStatus",
    "ProcessingPipelineDTO",
    "ProcessingPipelineStatus",
    "ProcessingTaskDTO",
    "ProcessingTaskStatus",
    "QueueError",
    "assemble",
]
