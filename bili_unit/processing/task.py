# task — pure type definitions for the processing task value.
# Runner reads/writes task values through the data store.

from dataclasses import dataclass, field

from . import (
    ProcessingPipelineStatus,
    ProcessingTaskStatus,
)


@dataclass
class PipelineEntry:
    """One pipeline (audio, and future subtitle/OCR) inside a processing task value.

    items: per-item-type rollup, e.g.
        {"transcription": {"total": 77, "completed": 77, "failed": 0, "skipped": 0}}
    """

    status: ProcessingPipelineStatus = ProcessingPipelineStatus.PENDING
    items: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class ProcessingTaskValue:
    uid: int
    status: ProcessingTaskStatus = ProcessingTaskStatus.PENDING
    pipelines: dict[str, PipelineEntry] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None

    def to_dict(self) -> dict:
        pipelines: dict[str, dict] = {}
        for name, entry in self.pipelines.items():
            pipelines[name] = {
                "status": entry.status.value,
                "items": entry.items,
            }
        return {
            "uid": self.uid,
            "status": self.status.value,
            "pipelines": pipelines,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProcessingTaskValue":
        pipelines: dict[str, PipelineEntry] = {}
        for name, entry in d.get("pipelines", {}).items():
            pipelines[name] = PipelineEntry(
                status=ProcessingPipelineStatus(entry["status"]),
                items=dict(entry.get("items", {})),
            )
        return cls(
            uid=d["uid"],
            status=ProcessingTaskStatus(d["status"]),
            pipelines=pipelines,
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )
