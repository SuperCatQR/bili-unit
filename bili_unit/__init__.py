# bili_unit — Bilibili unit top-level entry.
#
# The project is CLI-first: commands produce one per-uid SQLite file
# (``{uid}.raw.db``), and callers read it directly with sqlite3. A few
# top-level helpers remain for the CLI and advanced scripts, but the
# package does not present a Python query facade.

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as _pkg_version
from pathlib import Path

from ._db import UidContext, list_uids  # noqa: F401 — re-exported helpers
from ._db.paths import resolve as _resolve_paths
from ._env import BiliSettings, get_settings, reload_settings  # noqa: F401
from ._types import CredentialProvider  # noqa: F401
from .asr import (  # noqa: F401
    ASRCommand,
    ASRCommandResult,
    ASRError,
)
from .command import BiliCommand
from .fetching import (  # noqa: F401 — public re-exports (write-side DTO + errors)
    CommandResult,
    FetchingError,
    TaskResult,
    TaskStatus,
)
from .processing import (  # noqa: F401
    AudioError,
    ProcessingCommandResult,
    ProcessingError,
)
from .tui_spec import TUI_MVP_ACTIONS, TUI_MVP_PANELS  # noqa: F401
from .workbench import (  # noqa: F401
    BiliWorkbench,
    TaskStartCheck,
    assemble_workbench,
    workbench_session,
)

__version__ = _pkg_version("bili-unit")


# ---------------------------------------------------------------------------
# Path helpers — consumer-facing
# ---------------------------------------------------------------------------


def db_path(uid: int, settings: BiliSettings | None = None) -> Path:
    """Return the SQLite DB path for ``uid`` — the consumer contract.

    Open with::

        import sqlite3, bili_unit
        conn = sqlite3.connect(bili_unit.db_path(uid))
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT endpoint, item_id FROM raw_payload"
        ):
            ...

    The file may not yet exist if no fetch run has touched this uid.
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
    """Wire fetching + asr behind a unified :class:`BiliCommand`."""
    from .asr import assemble as _asr_assemble
    from .fetching import assemble as _fetching_assemble

    if settings is None:
        settings = get_settings()

    fetch_cmd = await _fetching_assemble(settings)
    proc_cmd = await _asr_assemble(
        settings,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )

    return BiliCommand(
        fetch_cmd,
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
    """Assemble + auto cleanup via async context manager::

        async with bili_unit.session() as cmd:
            await cmd.fetch(uid)
            await cmd.asr(uid)

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
    "ASRCommand",
    "ASRCommandResult",
    "ASRError",
    "BiliCommand",
    "BiliWorkbench",
    "BiliSettings",
    "CommandResult",
    "CredentialProvider",
    "FetchingError",
    "ProcessingCommandResult",
    "ProcessingError",
    "TaskResult",
    "TaskStartCheck",
    "TaskStatus",
    "TUI_MVP_ACTIONS",
    "TUI_MVP_PANELS",
    "UidContext",
    "__version__",
    "assemble",
    "assemble_workbench",
    "db_path",
    "get_settings",
    "list_uids",
    "reload_settings",
    "session",
    "workbench_session",
]
