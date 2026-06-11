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
from .query import BiliQuery


async def assemble() -> tuple[BiliCommand, BiliQuery, object, object]:
    """Unified assembly for the whole bili unit.

    Wires every stage's stores + components, then groups them behind the
    bili-unit-level :class:`BiliCommand` / :class:`BiliQuery` facades.

    Returns ``(cmd, qry, fetch_data, fetch_error)``. Stores are returned so
    the caller can ``await store.close()`` on shutdown. ``BiliCommand.close()``
    closes them all transitively.
    """
    from .fetching import assemble as _fetching_assemble
    from .processing.audio._asr_backend import create_asr_backend
    from .processing.command import ProcessingCommand
    from .processing.data import ProcessingDataStore
    from .processing.env import get_processing_settings
    from .processing.error import ProcessingErrorStore
    from .processing.query import ProcessingQuery

    fetch_cmd, fetch_qry, fetch_data, fetch_error = await _fetching_assemble()

    s = get_processing_settings()
    proc_data = ProcessingDataStore(s.bili_processing_data_dir)
    proc_error = ProcessingErrorStore(s.bili_processing_error_dir)
    await proc_data.open()
    await proc_error.open()

    asr_backend = create_asr_backend(s.bili_processing_asr_backend, settings=s)

    async def _close_processing_stores() -> None:
        await proc_data.close()
        await proc_error.close()
        if asr_backend is not None:
            await asr_backend.close()

    async def _close_fetching_stack() -> None:
        await _close_processing_stores()
        await fetch_data.close()
        await fetch_error.close()

    proc_cmd = ProcessingCommand(
        data=proc_data,
        error=proc_error,
        temp_dir=s.bili_processing_temp_dir,
        fetching_query=fetch_qry,
        settings=s,
        asr_backend=asr_backend,
        fetching_close=_close_fetching_stack,
    )
    proc_qry = ProcessingQuery(
        data=proc_data,
        error=proc_error,
        fetching_query=fetch_qry,
    )

    cmd = BiliCommand(fetch_cmd, processing=proc_cmd)
    qry = BiliQuery(fetch_qry, processing=proc_qry)
    return cmd, qry, fetch_data, fetch_error


__all__ = [
    "BiliCommand",
    "BiliQuery",
    "CommandResult",
    "EndpointDTO",
    "EndpointStatus",
    "ErrorDTO",
    "FetchingError",
    "TaskDTO",
    "TaskResult",
    "TaskStatus",
    "assemble",
]
