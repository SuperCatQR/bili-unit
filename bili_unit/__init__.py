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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from .processing.runner._audio import CredentialProvider  # noqa: F401
from .query import BiliQuery

__version__ = _pkg_version("bili-unit")


async def assemble(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> tuple[BiliCommand, BiliQuery]:
    """Unified assembly for the whole bili unit.

    Wires every stage's stores + components, then groups them behind the
    bili-unit-level :class:`BiliCommand` / :class:`BiliQuery` facades.

    Args:
        settings: pre-built :class:`BiliSettings` to use across all stages.
            ``None`` (default) lazy-loads from .env via :func:`get_settings` — this
            is the historical CLI path.
        asr_backend_override: when set, takes precedence over
            ``BILI_PROCESSING_ASR_BACKEND``. Lets the CLI pick ``mock`` for a run
            without editing .env, e.g. when only running transform.
        credential_provider: async callable returning a B站 ``Credential``.
            ``None`` (default) uses :func:`bili_unit.fetching.auth.get_credential`,
            which reads credentials from settings/env. Pass an explicit provider
            when embedding (e.g. credentials managed by the host application).

    Returns ``(cmd, qry)``. Call ``await cmd.close()`` on shutdown to release
    all stage resources in the correct order (processing → parsing → fetching).
    """
    from .fetching import assemble as _fetching_assemble
    from .fetching.auth import get_credential
    from .parsing.command import ParsingCommand
    from .parsing.data import ParsingDataStore
    from .parsing.query import ParsingQuery
    from .processing.audio._asr_backend import create_asr_backend
    from .processing.command import ProcessingCommand
    from .processing.data import ProcessingDataStore
    from .processing.error import ProcessingErrorStore
    from .processing.query import ProcessingQuery

    if settings is None:
        settings = get_settings()
    if credential_provider is None:
        credential_provider = get_credential

    fetch_cmd, fetch_qry, fetch_data, fetch_error = await _fetching_assemble(settings)

    # --- parsing layer ---
    parsing_data = ParsingDataStore(settings.bili_parsing_data_dir)
    await parsing_data.open()

    parse_cmd = ParsingCommand(data=parsing_data, fetching_query=fetch_qry)
    parse_qry = ParsingQuery(data=parsing_data)

    # --- processing layer ---
    proc_data = ProcessingDataStore(settings.bili_processing_data_dir)
    proc_error = ProcessingErrorStore(settings.bili_processing_error_dir)
    await proc_data.open()
    await proc_error.open()

    backend_name = asr_backend_override or settings.bili_processing_asr_backend
    asr_backend = create_asr_backend(backend_name, settings=settings)

    proc_cmd = ProcessingCommand(
        data=proc_data,
        error=proc_error,
        temp_dir=settings.bili_processing_temp_dir,
        fetching_query=fetch_qry,
        settings=settings,
        asr_backend=asr_backend,
        credential_provider=credential_provider,
    )
    proc_qry = ProcessingQuery(
        data=proc_data,
        error=proc_error,
        parsing_query=parse_qry,
    )

    cmd = BiliCommand(fetch_cmd, parsing=parse_cmd, processing=proc_cmd)
    qry = BiliQuery(fetch_qry, parsing=parse_qry, processing=proc_qry)
    return cmd, qry


@asynccontextmanager
async def session(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> AsyncIterator[tuple[BiliCommand, BiliQuery]]:
    """SDK-recommended entry: assemble + auto cleanup via async context manager.

    Equivalent to::

        cmd, qry = await assemble(settings, ...)
        try:
            yield cmd, qry
        finally:
            await cmd.close()

    The arguments are forwarded verbatim to :func:`assemble`; see that function's
    docstring for parameter semantics.

    Example::

        async with bili_unit.session() as (cmd, qry):
            await cmd.fetch(uid=123)
            task = await qry.fetching.get_task(uid=123)
    """
    cmd, qry = await assemble(
        settings,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )
    try:
        yield cmd, qry
    finally:
        await cmd.close()


__all__ = [
    "AudioError",
    "BiliCommand",
    "BiliQuery",
    "BiliSettings",
    "CommandResult",
    "CredentialProvider",
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
    "session",
]
