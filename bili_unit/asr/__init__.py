"""ASR stage public aliases.

The implementation still lives in ``bili_unit.processing`` while the project
transitions away from the older stage name. New code should import from this
package when it means audio transcription specifically.
"""

from ..processing import (
    ASRAPIError,
    ASRConfigError,
    ASRConnectionError,
    AudioError,
    AudioSizeError,
    ConvertError,
    DownloadError,
    ProcessingCommandResult,
    ProcessingError,
    ProcessingItemStatus,
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
    QueueError,
    assemble,
)
from ..processing.command import ProcessingCommand as ASRCommand
from ..processing.runner import ProcessingRunner as ASRRunner

ASRCommandResult = ProcessingCommandResult
ASRError = ProcessingError
ASRItemStatus = ProcessingItemStatus
ASRPipelineStatus = ProcessingPipelineStatus
ASRTaskStatus = ProcessingTaskStatus

__all__ = [
    "ASRAPIError",
    "ASRCommand",
    "ASRCommandResult",
    "ASRConfigError",
    "ASRConnectionError",
    "ASRError",
    "ASRItemStatus",
    "ASRPipelineStatus",
    "ASRRunner",
    "ASRTaskStatus",
    "AudioError",
    "AudioSizeError",
    "ConvertError",
    "DownloadError",
    "QueueError",
    "assemble",
]
