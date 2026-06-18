from __future__ import annotations

from pathlib import Path

import pytest

from bili_unit import BiliSettings
from bili_unit._db import UidContext
from bili_unit.command import BiliCommand
from bili_unit.fetching import CommandResult, TaskStatus
from bili_unit.service import BiliService, service_session


def _settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "processing_temp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr_cache"),
        bili_processing_asr_backend="mock",
    )


class _FetchCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[int, list[str] | None, str]] = []
        self.closed = False

    async def fetch_uid(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        self.calls.append((uid, endpoints, mode))
        return CommandResult(uid=uid, status=TaskStatus.SUCCESS, run_id="run-1")

    async def close(self) -> None:
        self.closed = True


async def test_service_lists_known_uids(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = Path(settings.bili_db_dir)
    root.mkdir(parents=True)
    (root / "42.db").write_text("", encoding="utf-8")
    (root / "42.raw.db").write_text("", encoding="utf-8")
    (root / "not-a-uid.db").write_text("", encoding="utf-8")

    service = BiliService(
        command=BiliCommand(_FetchCommand(), settings=settings),
        settings=settings,
    )

    assert service.list_uids() == [42]


async def test_service_delegates_write_commands(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fetch = _FetchCommand()
    service = BiliService(
        command=BiliCommand(fetch, settings=settings),
        settings=settings,
    )

    result = await service.fetch(123, endpoints=["user_info"], mode="full")

    assert result.status == TaskStatus.SUCCESS
    assert result.run_id == "run-1"
    assert fetch.calls == [(123, ["user_info"], "full")]


async def test_service_inspect_uid_degrades_when_db_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = BiliService(
        command=BiliCommand(_FetchCommand(), settings=settings),
        settings=settings,
    )

    snapshot = await service.inspect_uid(123)

    assert snapshot.uid == 123
    assert snapshot.run_summary is None
    assert snapshot.read_error == "main DB does not exist"


async def test_service_run_summary_reads_existing_db(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx = UidContext(uid=123, root=Path(settings.bili_db_dir))
    await ctx.open(raw=False)
    try:
        await ctx.main.execute(
            "INSERT INTO stage_task("
            "stage, status, payload, created_at_ms, updated_at_ms"
            ") VALUES (?, ?, ?, ?, ?)",
            ("fetching", "SUCCESS", '{"endpoints":[]}', 1, 2),
        )
    finally:
        await ctx.close()

    service = BiliService(
        command=BiliCommand(_FetchCommand(), settings=settings),
        settings=settings,
    )

    summary = await service.run_summary(123)

    assert summary.uid == 123
    assert summary.fetch.status == "SUCCESS"


async def test_service_can_start_task_allows_missing_uid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = BiliService(
        command=BiliCommand(_FetchCommand(), settings=settings),
        settings=settings,
    )

    check = await service.can_start_task(999)

    assert check.uid == 999
    assert check.can_start is True
    assert check.active_stages == ()
    assert check.reason is None


async def test_service_can_start_task_blocks_requested_running_stage(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    ctx = UidContext(uid=123, root=Path(settings.bili_db_dir))
    await ctx.open(raw=False)
    try:
        await ctx.main.execute(
            "INSERT INTO stage_task("
            "stage, status, payload, created_at_ms, updated_at_ms"
            ") VALUES (?, ?, ?, ?, ?)",
            ("fetching", "RUNNING", '{"endpoints":[]}', 1, 2),
        )
    finally:
        await ctx.close()

    service = BiliService(
        command=BiliCommand(_FetchCommand(), settings=settings),
        settings=settings,
    )

    blocked = await service.can_start_task(123, stages=("fetching",))
    allowed = await service.can_start_task(123, stages=("asr",))

    assert blocked.can_start is False
    assert blocked.active_stages == ("fetching",)
    assert blocked.requested_stages == ("fetching",)
    assert blocked.reason == "stage already running: fetching"
    assert allowed.can_start is True
    assert allowed.active_stages == ("fetching",)


async def test_service_session_closes_underlying_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bili_unit.service as service_module

    settings = _settings(tmp_path)
    fetch = _FetchCommand()
    fake_service = BiliService(
        command=BiliCommand(fetch, settings=settings),
        settings=settings,
    )

    async def fake_assemble_service(*_args, **_kwargs):
        return fake_service

    monkeypatch.setattr(
        service_module, "assemble_service", fake_assemble_service,
    )

    async with service_session(settings=settings) as service:
        assert service is fake_service

    assert fetch.closed is True
