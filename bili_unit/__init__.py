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

from ._aggregates import VideoFullDTO, VideoSummaryDTO  # noqa: F401
from ._env import BiliSettings, get_settings, reload_settings  # noqa: F401
from ._types import CredentialProvider  # noqa: F401
from .command import BiliCommand
from .fetching import (  # noqa: F401 – public re-exports
    CommandResult,
    EndpointDTO,
    EndpointStatus,
    FetchingError,
    FetchingErrorDTO,
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
    ProcessingErrorDTO,
    ProcessingItemDTO,
    ProcessingItemStatus,
    ProcessingPipelineDTO,
    ProcessingPipelineStatus,
    ProcessingTaskDTO,
    ProcessingTaskStatus,
)
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
    from .parsing import assemble as _parsing_assemble
    from .processing import assemble as _processing_assemble

    if settings is None:
        settings = get_settings()

    fetch_cmd, fetch_qry, _fetch_data, _fetch_error = await _fetching_assemble(settings)
    parse_cmd, parse_qry, _parse_data = await _parsing_assemble(
        settings, fetching_query=fetch_qry,
    )
    proc_cmd, proc_qry, _proc_data, _proc_error = await _processing_assemble(
        settings,
        fetching_query=fetch_qry,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
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
    "FetchingError",
    "FetchingErrorDTO",
    "ParsingCommandResult",
    "ParsingError",
    "ParsingImageDTO",
    "ParsingModelDTO",
    "ParsingModelStatus",
    "ParsingTaskDTO",
    "ParsingTaskStatus",
    "ProcessingCommandResult",
    "ProcessingError",
    "ProcessingErrorDTO",
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
