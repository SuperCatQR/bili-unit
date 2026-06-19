from __future__ import annotations

from io import StringIO

from bili_unit._cli_render import CliRenderer
from bili_unit.fetching import TaskStatus
from bili_unit.observability.summary import (
    AsrSummary,
    FetchEndpointSummary,
    FetchSummary,
    RunEventSummary,
    RunRecord,
    RunSummary,
)
from bili_unit.processing import ProcessingTaskStatus


def _lines(stream: StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def test_fetch_result_renders_status_value() -> None:
    stream = StringIO()
    renderer = CliRenderer(stream)

    renderer.fetch_result(uid=1, status=TaskStatus.SUCCESS)

    assert _lines(stream) == [
        "uid=1  status=SUCCESS",
    ]


def test_asr_result_renders_dry_run_budget_and_coverage() -> None:
    stream = StringIO()
    renderer = CliRenderer(stream)

    renderer.asr_result(
        uid=42,
        status=ProcessingTaskStatus.PARTIAL,
        candidates=["BVa", "BVb"],
        estimate={
            "item_count": 2,
            "page_count": 3,
            "audio_seconds": 123.4,
            "audio_tokens": 802,
        },
        budget_exceeded=["audio_tokens"],
        coverage={
            "success": 1,
            "expected": 3,
            "missing": 1,
            "failed": 1,
            "missing_bvids": ["BVm"],
            "failed_bvids": ["BVf"],
        },
    )

    assert _lines(stream) == [
        "uid=42  status=PARTIAL  (2 candidates)",
        "  estimate: items=2 pages=3 seconds=123.4 tokens=802",
        "  budget exceeded: audio_tokens",
        "  candidates: BVa, BVb",
        "  coverage: success=1/3 missing=1 failed=1",
        "  missing: BVm",
        "  failed: BVf",
    ]


def test_summary_rendering_surfaces_current_state_and_attention() -> None:
    stream = StringIO()
    renderer = CliRenderer(stream)
    summary = RunSummary(
        uid=42,
        run=RunRecord(
            run_id="run-1",
            uid=42,
            command="asr",
            status="PARTIAL",
            started_at_ms=1,
            ended_at_ms=2,
        ),
        stage_tasks={},
        fetch=FetchSummary(
            status="PARTIAL",
            endpoints=[
                FetchEndpointSummary(
                    endpoint="videos",
                    status="SUCCESS",
                    retry_count=0,
                    last_error_id=None,
                    item_progress=None,
                    progress=None,
                    updated_at_ms=1,
                ),
                FetchEndpointSummary(
                    endpoint="article_detail",
                    status="FAILED_EXHAUSTED",
                    retry_count=2,
                    last_error_id=1,
                    item_progress=None,
                    progress=None,
                    updated_at_ms=1,
                ),
            ],
        ),
        asr=AsrSummary(
            status="PARTIAL",
            candidate_count=3,
            expected=3,
            success=1,
            missing=1,
            failed=1,
            missing_bvids=["BVm"],
            failed_bvids=["BVf"],
            status_counts={"success": 1, "failed": 1},
        ),
        recent_events=[],
        recent_attention_events=[
            RunEventSummary(
                id=1,
                ts_ms=1,
                level="INFO",
                stage="asr",
                event="asr.item.retry_scheduled",
                endpoint=None,
                pipeline="audio",
                item_type="transcription",
                item_id="BVf",
                message=None,
                data={"retry": 1, "delay_s": 30},
            ),
            RunEventSummary(
                id=2,
                ts_ms=2,
                level="WARNING",
                stage="asr",
                event="asr.coverage.partial",
                endpoint=None,
                pipeline="audio",
                item_type=None,
                item_id=None,
                message=None,
                data={"missing": 1, "failed": 1},
            ),
        ],
    )

    renderer.asr_summary(summary)

    assert _lines(stream) == [
        "uid=42  status=PARTIAL  (3 candidates)",
        "  coverage: success=1/3 missing=1 failed=1",
        "  missing: BVm",
        "  failed: BVf",
        "  transcription rows: failed=1, success=1",
        "  recent attention:",
        "    asr.item.retry_scheduled BVf (retry=1 delay_s=30)",
        "    asr.coverage.partial audio (missing=1 failed=1)",
    ]


def test_fetch_summary_rendering() -> None:
    stream = StringIO()
    renderer = CliRenderer(stream)
    summary = RunSummary(
        uid=7,
        run=RunRecord(
            run_id="run-2",
            uid=7,
            command="fetch",
            status="SUCCESS",
            started_at_ms=1,
            ended_at_ms=2,
        ),
        stage_tasks={},
        fetch=FetchSummary(
            status="SUCCESS",
            endpoints=[
                FetchEndpointSummary("videos", "SUCCESS", 0, None, None, None, 1),
            ],
        ),
        asr=AsrSummary(),
        recent_events=[],
        recent_attention_events=[],
    )

    renderer.fetch_summary(summary)

    assert _lines(stream) == [
        "uid=7  status=SUCCESS",
        "  endpoints: SUCCESS=1",
    ]


def test_delete_rendering() -> None:
    stream = StringIO()
    renderer = CliRenderer(stream)

    renderer.delete_missing(uid=9)
    renderer.delete_cancelled()
    renderer.delete_stats({"raw_db": 1, "workdir_files": 0})

    assert _lines(stream) == [
        "uid=9: no data found",
        "Cancelled",
        "  raw_db=1, workdir_files=0",
    ]
