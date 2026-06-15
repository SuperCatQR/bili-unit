# bili_unit — Bilibili unit top-level entry.
#
# Per docs/refactor-plan-sqlite.md, the unit is being repositioned as a
# passive persistent data store: producer SDK (write side) on the inside,
# SQLite databases on the outside. Consumers query the DBs directly via
# ``sqlite3.connect(bili_unit.db_path(uid))`` — no Python query facade.
#
# Public surface:
#   __init__.py    — session()/assemble() write entry, db_path() helpers
#   __main__.py    — unified CLI (fetch / parse / process / delete-uid)
#   command/       — BiliCommand (write-side facade)
#
# Stage sub-packages (`fetching/`, `parsing/`, `processing/`) are internal.

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as _pkg_version
from pathlib import Path

from ._db import UidContext, list_uids  # noqa: F401 — re-exported helpers
from ._db.paths import resolve as _resolve_paths
from ._env import BiliSettings, get_settings, reload_settings  # noqa: F401
from ._types import CredentialProvider  # noqa: F401
from .command import BiliCommand
from .fetching import (  # noqa: F401 — public re-exports (write-side DTO + errors)
    CommandResult,
    FetchingError,
    TaskResult,
    TaskStatus,
)
from .parsing import (  # noqa: F401
    ParsingCommandResult,
    ParsingError,
)
from .processing import (  # noqa: F401
    AudioError,
    ProcessingCommandResult,
    ProcessingError,
)

__version__ = _pkg_version("bili-unit")


# ---------------------------------------------------------------------------
# Path helpers — consumer-facing
# ---------------------------------------------------------------------------

def db_path(uid: int, settings: BiliSettings | None = None) -> Path:
    """Return the main SQLite DB path for ``uid`` — the consumer contract.

    Open with::

        import sqlite3, bili_unit
        conn = sqlite3.connect(bili_unit.db_path(uid))
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM video"):
            ...

    The file may not yet exist if no fetch/parse run has touched this uid.
    """
    s = settings if settings is not None else get_settings()
    return _resolve_paths(uid, s.bili_db_dir).main_db


def raw_db_path(uid: int, settings: BiliSettings | None = None) -> Path:
    """Return the raw-payload DB path for ``uid``.

    Producer-private — most consumers do NOT need to open this. Use only when
    re-parsing raw B站 responses without re-fetching.
    """
    s = settings if settings is not None else get_settings()
    return _resolve_paths(uid, s.bili_db_dir).raw_db


# ---------------------------------------------------------------------------
# Assembly + session lifecycle
# ---------------------------------------------------------------------------

async def assemble(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> BiliCommand:
    """Wire every stage's command behind a unified BiliCommand.

    Phase 3+ contract: returns a single ``BiliCommand``. The legacy
    ``(cmd, qry)`` tuple is gone — read side is consumer's SQL.
    """
    from .fetching import assemble as _fetching_assemble
    from .parsing import assemble as _parsing_assemble
    from .processing import assemble as _processing_assemble

    if settings is None:
        settings = get_settings()

    fetch_cmd = await _fetching_assemble(settings)
    parse_cmd = await _parsing_assemble(settings)
    proc_cmd = await _processing_assemble(
        settings,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )

    return BiliCommand(
        fetch_cmd,
        parsing=parse_cmd,
        processing=proc_cmd,
        settings=settings,
    )


@asynccontextmanager
async def session(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> AsyncIterator[BiliCommand]:
    """SDK-recommended entry: assemble + auto cleanup via async context manager.

    Phase 3+ contract: yields a single ``BiliCommand``::

        async with bili_unit.session() as cmd:
            await cmd.fetch(uid)
            await cmd.parse(uid)

    Read side is on the database file — open it directly with
    :func:`db_path` / :func:`sqlite3.connect`.
    """
    cmd = await assemble(
        settings,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )
    try:
        yield cmd
    finally:
        await cmd.close()


__all__ = [
    "AudioError",
    "BiliCommand",
    "BiliSettings",
    "CommandResult",
    "CredentialProvider",
    "FetchingError",
    "ParsingCommandResult",
    "ParsingError",
    "ProcessingCommandResult",
    "ProcessingError",
    "TaskResult",
    "TaskStatus",
    "UidContext",
    "__version__",
    "assemble",
    "db_path",
    "get_settings",
    "list_uids",
    "raw_db_path",
    "reload_settings",
    "session",
]
