# bili_unit/parsing — common DTOs, exceptions, and assembly root.
#
# The parsing layer sits between fetching (raw dicts) and processing
# (structured results).  It converts raw API payloads into 5 typed
# dataclass models and persists them as JSON.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .._storage import DecodeError as _DecodeError

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


class DataError(_DecodeError, ParsingError):
    """Storage / serialisation failure."""


class ImageDownloadError(ParsingError):
    """Image download failure."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class ParsingModelDTO:
    model: str
    status: ParsingModelStatus
    count: int = 0


@dataclass
class ParsingImageDTO:
    total: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    failed_urls: list[str] = field(default_factory=list)


@dataclass
class ParsingTaskDTO:
    uid: int
    status: ParsingTaskStatus
    models: dict[str, ParsingModelDTO] = field(default_factory=dict)
    images: ParsingImageDTO | None = None
    created_at: int | None = None
    updated_at: int | None = None
    failed_item_ids: list[str] = field(default_factory=list)
    """Names of models that ended in ``ParsingModelStatus.FAILED``.

    Parsing has no ErrorStore so the granularity is per-model rather than
    per-item; entries are bare model names (e.g. ``"article_post"``).
    """


@dataclass
class ParsingCommandResult:
    uid: int
    status: ParsingTaskStatus


# ---------------------------------------------------------------------------
# Task value (pure type, no I/O)
# ---------------------------------------------------------------------------

@dataclass
class ParsingTaskValue:
    uid: int
    status: ParsingTaskStatus = ParsingTaskStatus.PENDING
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    images: dict[str, Any] | None = None
    created_at: int | None = None
    updated_at: int | None = None
    failed_item_ids: list[str] = field(default_factory=list)
    """Names of models that ended in ``FAILED``; aggregated at task finalisation.

    Empty in mid-flight ``ParsingTaskValue`` instances — the command writes the
    final list when ``parse_uid`` finalises the task.
    """

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "uid": self.uid,
            "status": self.status.value,
            "models": {},
        }
        for model, entry in self.models.items():
            d["models"][model] = {
                "status": entry.get("status", "PENDING"),
                "count": entry.get("count", 0),
            }
        if self.images is not None:
            d["images"] = self.images
        d["created_at"] = self.created_at
        d["updated_at"] = self.updated_at
        d["failed_item_ids"] = list(self.failed_item_ids)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ParsingTaskValue:
        models: dict[str, dict[str, Any]] = {}
        for model, entry in d.get("models", {}).items():
            models[model] = dict(entry)
        return cls(
            uid=d["uid"],
            status=ParsingTaskStatus(d.get("status", "PENDING")),
            models=models,
            images=d.get("images"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            failed_item_ids=list(d.get("failed_item_ids", [])),
        )


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
    "DataError",
    "ImageDownloadError",
    "ModelParseError",
    "ParsingCommandResult",
    "ParsingError",
    "ParsingImageDTO",
    "ParsingModelDTO",
    "ParsingModelStatus",
    "ParsingTaskDTO",
    "ParsingTaskStatus",
    "ParsingTaskValue",
    "assemble",
]
