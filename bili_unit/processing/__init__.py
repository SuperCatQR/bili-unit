# bili_unit/processing — common DTOs, exceptions.

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .._storage import DecodeError as _DecodeError

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


class DataError(_DecodeError, ProcessingError):
    """存储 / 序列化失败。"""


# ---------------------------------------------------------------------------
# DTOs
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
# Assembly root — opens processing stores, wires dependencies, picks ASR backend
# ---------------------------------------------------------------------------

async def assemble(
    settings,
    *,
    fetching_query,
    parsing_query=None,
    asr_backend_override: str | None = None,
    credential_provider=None,
):
    """Open processing stores, wire dependencies, return ``(cmd, qry, data, error)``.

    Args:
        settings: ``BiliSettings`` already loaded by the caller.
        fetching_query: a :class:`FetchingReadView`-shaped object.
        parsing_query: optional :class:`ParsingQuery` — when provided, audio
            pipeline can short-circuit ASR for bvids whose ``video_subtitle``
            is already complete in parsing storage.
        asr_backend_override: takes precedence over BILI_PROCESSING_ASR_BACKEND.
        credential_provider: async callable returning a ``Credential | None``;
            defaults to ``bili_unit.fetching.auth.get_credential`` when None.

    Caller is responsible for closing the returned stores via cmd.close().
    """
    from ..fetching.auth import get_credential
    from .audio._asr_backend import create_asr_backend
    from .command import ProcessingCommand
    from .data import ProcessingDataStore
    from .error import ProcessingErrorStore
    from .query import ProcessingQuery

    data = ProcessingDataStore(settings.bili_processing_data_dir)
    error = ProcessingErrorStore(settings.bili_processing_error_dir)
    await data.open()
    await error.open()

    backend_name = asr_backend_override or settings.bili_processing_asr_backend
    asr_backend = create_asr_backend(backend_name, settings=settings)

    if credential_provider is None:
        credential_provider = get_credential

    cmd = ProcessingCommand(
        data=data,
        error=error,
        temp_dir=settings.bili_processing_temp_dir,
        fetching_query=fetching_query,
        parsing_query=parsing_query,
        settings=settings,
        asr_backend=asr_backend,
        credential_provider=credential_provider,
    )
    qry = ProcessingQuery(data=data, error=error)
    return cmd, qry, data, error


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
