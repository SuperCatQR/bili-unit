from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import bili_unit
from bili_unit import __main__ as cli
from bili_unit.fetching import CommandResult, TaskStatus
from bili_unit.observability.summary import (
    AsrSummary,
    FetchSummary,
    RunSummary,
)
from bili_unit.processing import ProcessingCommandResult, ProcessingTaskStatus


@dataclass
class _FakeSession:
    command: object

    async def __aenter__(self):
        return self.command

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeCommand:
    async def fetch(self, uid: int, *, endpoints=None, mode="incremental"):
        return CommandResult(uid=uid, status=TaskStatus.SUCCESS, run_id="fetch-run-1")

    async def asr(self, uid: int, **_kwargs):
        return ProcessingCommandResult(
            uid=uid,
            status=ProcessingTaskStatus.PARTIAL,
            run_id="asr-run-1",
            coverage={
                "expected": 2,
                "success": 1,
                "missing": 1,
                "failed": 0,
                "missing_bvids": ["BVm"],
                "failed_bvids": [],
            },
        )


async def test_fetch_handler_falls_back_when_summary_unavailable(
    monkeypatch,
    capsys,
) -> None:
    calls = []
    monkeypatch.setattr(
        bili_unit,
        "session",
        lambda **_kwargs: _FakeSession(_FakeCommand()),
    )

    async def fake_summary(
        uid: int,
        *,
        run_id: str | None = None,
        filter_events_to_run: bool = True,
    ):
        calls.append((uid, run_id, filter_events_to_run))
        return None

    monkeypatch.setattr(cli, "_load_cli_summary", fake_summary)

    args = argparse.Namespace(
        uid=123,
        mode="incremental",
        endpoints=["videos"],
        exclude_endpoints=None,
        profile="all",
    )

    await cli._handle_fetch(args)

    assert capsys.readouterr().out.splitlines() == [
        "uid=123  status=SUCCESS",
    ]
    assert calls == [(123, "fetch-run-1", True)]


async def test_asr_handler_prefers_run_summary(
    monkeypatch,
    capsys,
) -> None:
    calls = []
    monkeypatch.setattr(
        bili_unit,
        "session",
        lambda **_kwargs: _FakeSession(_FakeCommand()),
    )

    async def fake_summary(uid: int, *, run_id: str | None = None):
        calls.append((uid, run_id))
        return await _summary_with_asr_gap(uid, run_id=run_id)

    monkeypatch.setattr(cli, "_load_cli_summary", fake_summary)

    args = argparse.Namespace(
        uid=123,
        mode="incremental",
        asr_backend=None,
        limit=None,
        only_bvids=None,
        exclude_bvids=None,
        retry_failed_only=False,
        dry_run=False,
        max_audio_seconds=None,
        max_audio_tokens=None,
    )

    await cli._handle_asr(args)

    assert capsys.readouterr().out.splitlines() == [
        "uid=123  status=PARTIAL  (2 candidates)",
        "  coverage: success=1/2 missing=1 failed=0",
        "  missing: BVm",
    ]
    assert calls == [(123, "asr-run-1")]


async def test_delete_uid_handler_deletes_orphan_workdir(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    uid = 123
    workdir = tmp_path / str(uid)
    workdir.mkdir()
    (workdir / "cover.jpg").write_bytes(b"x")

    monkeypatch.setattr(
        bili_unit,
        "db_path",
        lambda _uid: tmp_path / f"{uid}.raw.db",
    )

    async def fake_delete_uid(_uid):
        assert _uid == uid
        assert workdir.exists()
        for path in workdir.rglob("*"):
            if path.is_file():
                path.unlink()
        workdir.rmdir()
        return {"raw_db": 0, "workdir_files": 1}

    command = _FakeCommand()
    command.delete_uid = fake_delete_uid
    monkeypatch.setattr(
        bili_unit,
        "session",
        lambda **_kwargs: _FakeSession(command),
    )

    await cli._handle_delete_uid(argparse.Namespace(uid=uid, yes=True))

    assert not workdir.exists()
    assert capsys.readouterr().out.splitlines() == [
        "  raw_db=0, workdir_files=1",
    ]


async def test_load_cli_summary_passes_run_id(monkeypatch) -> None:
    calls = []

    class _Settings:
        bili_db_dir = "db-root"

    async def fake_load_run_summary(*, uid, root, run_id=None, recent_limit=20):
        calls.append((uid, root, run_id, recent_limit))
        return await _summary_with_asr_gap(uid, run_id=run_id)

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(cli, "load_run_summary", fake_load_run_summary)

    summary = await cli._load_cli_summary(123, run_id="run-exact")

    assert summary is not None
    assert calls == [(123, "db-root", "run-exact", 12)]


async def test_load_cli_summary_logs_fallback_reason(monkeypatch, caplog) -> None:
    class _Settings:
        bili_db_dir = "db-root"

    async def fake_load_run_summary(**_kwargs):
        raise RuntimeError("summary boom")

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(cli, "load_run_summary", fake_load_run_summary)

    with caplog.at_level("DEBUG", logger="bili.cli"):
        summary = await cli._load_cli_summary(123, run_id="run-exact")

    assert summary is None
    assert "cli_summary_load_failed" in caplog.text
    assert "summary boom" in caplog.text


async def _summary_with_asr_gap(uid: int, *, run_id: str | None = None) -> RunSummary:
    return RunSummary(
        uid=uid,
        run=None,
        stage_tasks={},
        fetch=FetchSummary(),
        asr=AsrSummary(
            status="PARTIAL",
            candidate_count=2,
            expected=2,
            success=1,
            missing=1,
            missing_bvids=["BVm"],
        ),
        recent_events=[],
        recent_attention_events=[],
    )
