# task — pure type definitions; no I/O.
# Runner reads/writes task values through the data store.

from dataclasses import dataclass, field

from . import EndpointStatus, TaskStatus

# ---------------------------------------------------------------------------
# Endpoint entry (one per uid:task -> endpoints dict value)
# ---------------------------------------------------------------------------

@dataclass
class EndpointEntry:
    status: EndpointStatus = EndpointStatus.PENDING
    retry_count: int = 0
    last_error_id: int | None = None
    item_progress: dict | None = None


# ---------------------------------------------------------------------------
# Task value shape stored under uid:{uid}:task
# ---------------------------------------------------------------------------

@dataclass
class TaskValue:
    uid: int
    status: TaskStatus = TaskStatus.PENDING
    endpoints: dict[str, EndpointEntry] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None
    failed_item_ids: list[str] = field(default_factory=list)
    """Aggregated identifiers of failed work units, written at task finalisation.

    Items are encoded as either ``"endpoint"`` (uid-level endpoint failure) or
    ``"endpoint:item_id"`` (item-level fan-out failure). The runner derives this
    list from ErrorStore + endpoint entries when persisting the final task value;
    in-flight ``TaskValue`` instances normally carry an empty list.
    """

    def to_dict(self) -> dict:
        eps = {}
        for k, v in self.endpoints.items():
            eps[k] = {
                "status": v.status.value,
                "retry_count": v.retry_count,
                "last_error_id": v.last_error_id,
            }
            if v.item_progress is not None:
                eps[k]["item_progress"] = v.item_progress
        return {
            "uid": self.uid,
            "status": self.status.value,
            "endpoints": eps,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failed_item_ids": list(self.failed_item_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskValue":
        endpoints = {}
        for k, v in d.get("endpoints", {}).items():
            endpoints[k] = EndpointEntry(
                status=EndpointStatus(v["status"]),
                retry_count=v.get("retry_count", 0),
                last_error_id=v.get("last_error_id"),
                item_progress=v.get("item_progress"),
            )
        return cls(
            uid=d["uid"],
            status=TaskStatus(d.get("status", "PENDING")),
            endpoints=endpoints,
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            failed_item_ids=list(d.get("failed_item_ids", [])),
        )
