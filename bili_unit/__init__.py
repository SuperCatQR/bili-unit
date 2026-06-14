# bili_unit — Bilibili unit top-level entry.
#
# Per docs/structure/bili.md §10, the bili unit exposes:
#   __init__.py    — DTO、异常、assemble() 装配
#   __main__.py    — 统一 CLI 入口
#   command/       — 写侧统一入口 (BiliCommand)
#   query/         — 只读统一入口 (BiliQuery)
#
# Stage sub-packages (`fetching/`, `processing/`) live behind the
# command/query facade and should not be reached from outside the bili unit.

from importlib.metadata import version as _pkg_version

from ._env import BiliSettings, get_settings, reload_settings  # noqa: F401
from .command import BiliCommand
from .fetching import (  # noqa: F401 – public re-exports
    CommandResult,
    EndpointDTO,
    EndpointStatus,
    ErrorDTO,
    FetchingError,
    TaskDTO,
    TaskResult,
    TaskStatus,
)
from .parsing import (  # noqa: F401 – public re-exports
    ParsingCommandResult,
    ParsingError,
    ParsingImageDTO,
    ParsingModelDTO,
    ParsingModelStatus,
    ParsingTaskDTO,
    ParsingTaskStatus,
)
from .processing import (  # noqa: F401 – public re-exports
    AudioError,
    ProcessingCommandResult,
    ProcessingError,
    ProcessingItemDTO,
    ProcessingItemStatus,
    ProcessingPipelineDTO,
    ProcessingPipelineStatus,
    ProcessingTaskDTO,
    ProcessingTaskStatus,
    VideoFullDTO,
    VideoSummaryDTO,
)
from .query import BiliQuery

__version__ = _pkg_version("bili-unit")


async def assemble(
    asr_backend_override: str | None = None,
) -> tuple[BiliCommand, BiliQuery]:
    """Unified assembly for the whole bili unit.

    Wires every stage's stores + components, then groups them behind the
    bili-unit-level :class:`BiliCommand` / :class:`BiliQuery` facades.

    Returns ``(cmd, qry)``. Call ``await cmd.close()`` on shutdown to release
    all stage resources in the correct order (processing → parsing → fetching).

    Args:
        asr_backend_override: when set, takes precedence over
            ``BILI_PROCESSING_ASR_BACKEND``. Lets the CLI pick ``mock`` for a
            run without editing .env, e.g. when only running transform.
    """
    from .fetching import assemble as _fetching_assemble
    from .fetching.auth import get_credential
    from .parsing.command import ParsingCommand
    from .parsing.data import ParsingDataStore
    from .parsing.env import get_parsing_settings
    from .parsing.query import ParsingQuery
    from .processing.audio._asr_backend import create_asr_backend
    from .processing.command import ProcessingCommand
    from .processing.data import ProcessingDataStore
    from .processing.env import get_processing_settings
    from .processing.error import ProcessingErrorStore
    from .processing.query import ProcessingQuery

    fetch_cmd, fetch_qry, fetch_data, fetch_error = await _fetching_assemble()

    # --- parsing layer ---
    ps = get_parsing_settings()
    parsing_data = ParsingDataStore(ps.bili_parsing_data_dir)
    await parsing_data.open()

    parse_cmd = ParsingCommand(data=parsing_data, fetching_query=fetch_qry)
    parse_qry = ParsingQuery(data=parsing_data)

    # --- processing layer ---
    s = get_processing_settings()
    proc_data = ProcessingDataStore(s.bili_processing_data_dir)
    proc_error = ProcessingErrorStore(s.bili_processing_error_dir)
    await proc_data.open()
    await proc_error.open()

    backend_name = asr_backend_override or s.bili_processing_asr_backend
    asr_backend = create_asr_backend(backend_name, settings=s)

    proc_cmd = ProcessingCommand(
        data=proc_data,
        error=proc_error,
        temp_dir=s.bili_processing_temp_dir,
        fetching_query=fetch_qry,
        settings=s,
        asr_backend=asr_backend,
        credential_provider=get_credential,
    )
    proc_qry = ProcessingQuery(
        data=proc_data,
        error=proc_error,
        parsing_query=parse_qry,
    )

    cmd = BiliCommand(fetch_cmd, parsing=parse_cmd, processing=proc_cmd)
    qry = BiliQuery(fetch_qry, parsing=parse_qry, processing=proc_qry)
    return cmd, qry


__all__ = [
    "AudioError",
    "BiliCommand",
    "BiliQuery",
    "BiliSettings",
    "CommandResult",
    "EndpointDTO",
    "EndpointStatus",
    "ErrorDTO",
    "FetchingError",
    "ParsingCommandResult",
    "ParsingError",
    "ParsingImageDTO",
    "ParsingModelDTO",
    "ParsingModelStatus",
    "ParsingTaskDTO",
    "ParsingTaskStatus",
    "ProcessingCommandResult",
    "ProcessingError",
    "ProcessingItemDTO",
    "ProcessingItemStatus",
    "ProcessingPipelineDTO",
    "ProcessingPipelineStatus",
    "ProcessingTaskDTO",
    "ProcessingTaskStatus",
    "TaskDTO",
    "TaskResult",
    "TaskStatus",
    "VideoFullDTO",
    "VideoSummaryDTO",
    "__version__",
    "assemble",
    "get_settings",
    "reload_settings",
]
