# tests for bili_unit/fetching/task
# Run: uv run pytest bili_unit/tests/test_task.py -v

from bili_unit.fetching import (
    EndpointStatus,
    TaskStatus,
)
from bili_unit.fetching.task import EndpointEntry, TaskValue


def test_task_value_roundtrip():
    tv = TaskValue(uid=1, status=TaskStatus.RUNNING)
    tv.endpoints["user_info"] = EndpointEntry(status=EndpointStatus.SUCCESS)
    tv.endpoints["videos"] = EndpointEntry(
        status=EndpointStatus.FAILED_RETRYABLE, retry_count=1, last_error_id=5,
    )
    tv.created_at = 1710000000000
    tv.updated_at = 1710000001000

    d = tv.to_dict()
    restored = TaskValue.from_dict(d)

    assert restored.uid == 1
    assert restored.status == TaskStatus.RUNNING
    assert restored.endpoints["user_info"].status == EndpointStatus.SUCCESS
    assert restored.endpoints["videos"].retry_count == 1
    assert restored.endpoints["videos"].last_error_id == 5
