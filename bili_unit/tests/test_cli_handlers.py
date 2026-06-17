from __future__ import annotations

import argparse
from dataclasses import dataclass

import bili_unit
from bili_unit import __main__ as cli
from bili_unit.fetching import CommandResult, TaskStatus
from bili_unit.observability.summary import (
    AsrSummary,
    FetchEndpointSummary,
    FetchSummary,
    ParseModelSummary,
    ParseSummary,
    RunSummary,
)
from bili_unit.parsing import ParsingCommandResult, ParsingTaskStatus
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

    async def parse(
        self,
        uid: int,
        *,
        mode="full",
        models=None,
        download_images=False,
    ):
        return ParsingCommandResult(
            uid=uid,
            status=ParsingTaskStatus.SUCCESS,
            run_id="parse-run-1",
        )

    async def sync(
        self,
        uid: int,
        *,
        endpoints=None,
        fetch_mode="incremental",
        parse_mode="full",
        download_images=False,
    ):
        from bili_unit.command import SyncCommandResult

        return SyncCommandResult(
            uid=uid,
            status="PARTIAL",
            fetch=CommandResult(
                uid=uid,
                status=TaskStatus.PARTIAL,
                run_id="fetch-run-1",
            ),
            parse=ParsingCommandResult(
                uid=uid,
                status=ParsingTaskStatus.SUCCESS,
                run_id="parse-run-1",
            ),
            run_id="parse-run-1",
        )

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

    async def fake_summary(uid: int, *, run_id: str | None = None):
        calls.append((uid, run_id))
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
    assert calls == [(123, "fetch-run-1")]


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


async def test_parse_handler_passes_run_id(
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
        return RunSummary(
            uid=uid,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(),
            parse=ParseSummary(
                status="SUCCESS",
                models=[
                    ParseModelSummary(
                        model="video_work",
                        status="SUCCESS",
                        count=1,
                    ),
                ],
            ),
            asr=AsrSummary(),
            recent_events=[],
            recent_attention_events=[],
        )

    monkeypatch.setattr(cli, "_load_cli_summary", fake_summary)

    args = argparse.Namespace(
        uid=123,
        mode="full",
        models=None,
        exclude_models=None,
        download_images=False,
    )

    await cli._handle_parse(args)

    assert capsys.readouterr().out.splitlines() == [
        "uid=123  status=SUCCESS",
        "  models: SUCCESS=1",
    ]
    assert calls == [(123, "parse-run-1")]


async def test_sync_handler_passes_run_id_and_keeps_workflow_status(
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
        return RunSummary(
            uid=uid,
            run=None,
            stage_tasks={},
            fetch=FetchSummary(
                status="PARTIAL",
                endpoints=[
                    FetchEndpointSummary(
                        endpoint="videos",
                        status="PARTIAL",
                        retry_count=0,
                        last_error_id=None,
                        item_progress=None,
                        progress=None,
                        updated_at_ms=1,
                    ),
                ],
            ),
            parse=ParseSummary(
                status="SUCCESS",
                models=[
                    ParseModelSummary(
                        model="video_work",
                        status="SUCCESS",
                        count=1,
                    ),
                ],
            ),
            asr=AsrSummary(),
            recent_events=[],
            recent_attention_events=[],
        )

    monkeypatch.setattr(cli, "_load_cli_summary", fake_summary)

    args = argparse.Namespace(
        uid=123,
        fetch_mode="incremental",
        parse_mode="full",
        endpoints=None,
        exclude_endpoints=None,
        profile="all",
        download_images=False,
    )

    await cli._handle_sync(args)

    assert capsys.readouterr().out.splitlines() == [
        "uid=123  status=PARTIAL  fetch=PARTIAL  parse=SUCCESS",
        "  endpoints: PARTIAL=1",
        "  models: SUCCESS=1",
    ]
    assert calls == [(123, "parse-run-1")]


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


async def _summary_none(uid: int, *, run_id: str | None = None):
    return None


async def _summary_with_asr_gap(uid: int, *, run_id: str | None = None) -> RunSummary:
    return RunSummary(
        uid=uid,
        run=None,
        stage_tasks={},
        fetch=FetchSummary(),
        parse=ParseSummary(),
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
