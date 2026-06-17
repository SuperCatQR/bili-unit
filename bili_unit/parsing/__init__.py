# bili_unit/parsing — common DTOs, exceptions, and assembly root.
#
# The parsing layer sits between fetching (raw dicts) and processing
# (structured results).  It converts raw API payloads into typed
# dataclass models and persists them to the per-uid SQLite main DB.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._env import BiliSettings
    from .command import ParsingCommand

# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class ParsingTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ParsingModelStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ParsingError(Exception):
    """Base for all parsing-layer exceptions."""


class ModelParseError(ParsingError):
    """Failed to parse a raw dict into a typed model."""


class ImageDownloadError(ParsingError):
    """Image download failure."""


# ---------------------------------------------------------------------------
# Write-side DTO
# ---------------------------------------------------------------------------

@dataclass
class ParsingCommandResult:
    uid: int
    status: ParsingTaskStatus


# ---------------------------------------------------------------------------
# Assembly root — returns the ParsingCommand
# ---------------------------------------------------------------------------

async def assemble(settings: BiliSettings) -> ParsingCommand:
    """Build a :class:`ParsingCommand` bound to ``settings``.

    Phase 3 contract: parsing no longer holds a long-lived store. The
    :class:`ParsingCommand` opens a per-uid SQLite context inside
    ``parse_uid`` and closes it on return. Caller still owns the lifetime
    of the command via ``await cmd.close()`` (a no-op today, kept for
    forward compatibility with future cross-uid resources).
    """
    from .command import ParsingCommand

    return ParsingCommand(settings)


__all__ = [
    "ImageDownloadError",
    "ModelParseError",
    "ParsingCommandResult",
    "ParsingError",
    "ParsingModelStatus",
    "ParsingTaskStatus",
    "assemble",
]
