from __future__ import annotations

from bili_unit.command import BiliCommand
from bili_unit.fetching import CommandResult, TaskStatus
from bili_unit.parsing import ParsingCommandResult, ParsingTaskStatus


class _FetchCommand:
    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        self.calls = []

    async def fetch_uid(self, uid, endpoints, mode="incremental"):
        self.calls.append((uid, endpoints, mode))
        return CommandResult(uid=uid, status=self.status)

    async def close(self):
        pass


class _ParseCommand:
    def __init__(self, status: ParsingTaskStatus = ParsingTaskStatus.SUCCESS) -> None:
        self.status = status
        self.calls = []

    async def parse_uid(self, uid, mode="full", models=None, download_images=False):
        self.calls.append((uid, mode, models, download_images))
        return ParsingCommandResult(uid=uid, status=self.status)

    async def close(self):
        pass


async def test_sync_runs_fetch_then_parse_with_modes():
    fetch = _FetchCommand(TaskStatus.SUCCESS)
    parse = _ParseCommand()
    cmd = BiliCommand(fetch, parsing=parse)

    result = await cmd.sync(
        123,
        endpoints=["user_info"],
        fetch_mode="refresh",
        parse_mode="incremental",
        parse_models=["video_work"],
        download_images=True,
    )

    assert result.status == "SUCCESS"
    assert result.fetch.status == TaskStatus.SUCCESS
    assert result.parse is not None
    assert result.parse.status == ParsingTaskStatus.SUCCESS
    assert fetch.calls == [(123, ["user_info"], "refresh")]
    assert parse.calls == [(123, "incremental", ["video_work"], True)]


async def test_sync_continues_parse_after_partial_fetch():
    fetch = _FetchCommand(TaskStatus.PARTIAL)
    parse = _ParseCommand()
    cmd = BiliCommand(fetch, parsing=parse)

    result = await cmd.sync(123)

    assert result.status == "PARTIAL"
    assert result.fetch.status == TaskStatus.PARTIAL
    assert result.parse is not None
    assert parse.calls == [(123, "full", None, False)]


async def test_sync_skips_parse_after_hard_fetch_failure():
    fetch = _FetchCommand(TaskStatus.FAILED_PERMANENT)
    parse = _ParseCommand()
    cmd = BiliCommand(fetch, parsing=parse)

    result = await cmd.sync(123)

    assert result.status == "FAILED_PERMANENT"
    assert result.parse is None
    assert parse.calls == []
