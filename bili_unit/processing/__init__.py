# bili_unit/processing — common DTOs, exceptions.
#
# Per docs/structure/bili.md §4/§6/§8 and docs/design/processing.md.

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Status enums (cf. processing design §10.4)
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
    """Status of a single pipeline (transform / audio) within a task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


# ---------------------------------------------------------------------------
# Exceptions (cf. processing design §9.1)
# ---------------------------------------------------------------------------

class ProcessingError(Exception):
    """Base for all processing-layer exceptions."""


class TransformError(ProcessingError):
    """transform 阶段错误。"""


class FieldExtractionError(TransformError):
    """字段提取失败。"""


class FormatError(TransformError):
    """格式异常。"""


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
    """存储 / 序列化失败。"""


# ---------------------------------------------------------------------------
# DTOs (cf. processing design §11.3)
# ---------------------------------------------------------------------------

@dataclass
class ErrorDTO:
    """Read-only error record returned by ProcessingQuery.list_errors()."""

    id: int
    uid: int | None
    pipeline: str | None
    item_type: str | None
    item_id: str | None
    error_type: str
    message: str
    retryable: str  # "true" | "false" | "unknown"
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
    errors: list[ErrorDTO] = field(default_factory=list)


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


@dataclass
class VideoFullDTO:
    bvid: str
    metadata: ProcessingItemDTO | None = None
    transcription: ProcessingItemDTO | None = None


@dataclass
class VideoSummaryDTO:
    bvid: str
    title: str
    status: ProcessingItemStatus
    has_transcription: bool
    duration: int | None = None


@dataclass
class ProcessingCommandResult:
    uid: int
    status: ProcessingTaskStatus


__all__ = [
    "ASRAPIError",
    "ASRConfigError",
    "ASRConnectionError",
    "AudioError",
    "AudioSizeError",
    "ConvertError",
    "DataError",
    "DownloadError",
    "ErrorDTO",
    "FieldExtractionError",
    "FormatError",
    "ProcessingCommandResult",
    "ProcessingError",
    "ProcessingItemDTO",
    "ProcessingItemStatus",
    "ProcessingPipelineDTO",
    "ProcessingPipelineStatus",
    "ProcessingTaskDTO",
    "ProcessingTaskStatus",
    "QueueError",
    "TransformError",
    "VideoFullDTO",
    "VideoSummaryDTO",
]
