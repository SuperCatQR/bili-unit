# bili_unit/parsing — common DTOs, exceptions, and assembly root.
#
# The parsing layer sits between fetching (raw dicts) and processing
# (structured results).  It converts raw API payloads into 5 typed
# dataclass models and persists them as JSON.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

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


class DataError(ParsingError):
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
        )


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
]
