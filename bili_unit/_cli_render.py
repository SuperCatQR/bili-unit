"""Human CLI result rendering.

Command handlers do orchestration; this module owns final stdout formatting.
Interactive setup/login prompts intentionally stay close to their workflows.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, TextIO

from .observability.summary import RunSummary


class CliRenderer:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def line(self, text: str = "") -> None:
        print(text, file=self._stream)

    def fetch_result(self, *, uid: int, status: Any) -> None:
        self.line(f"uid={uid}  status={_status_value(status)}")

    def fetch_summary(self, summary: RunSummary, *, fallback_status: Any = None) -> None:
        status = summary.fetch.status
        if status is None and summary.run is not None:
            status = summary.run.status
        if status is None:
            status = fallback_status
        self.line(f"uid={summary.uid}  status={_status_value(status)}")
        if summary.fetch.endpoints:
            counts = _format_counts(summary.fetch.status_counts)
            if counts:
                self.line(f"  endpoints: {counts}")
            failed = [
                endpoint.endpoint
                for endpoint in summary.fetch.endpoints
                if _is_failure_status(endpoint.status)
            ]
            if failed:
                self.line(f"  failed endpoints: {', '.join(failed)}")
        self.attention_events(summary)

    def asr_result(
        self,
        *,
        uid: int,
        status: Any,
        candidates: list[str] | None = None,
        estimate: Mapping[str, Any] | None = None,
        budget_exceeded: list[str] | None = None,
        coverage: Mapping[str, Any] | None = None,
    ) -> None:
        if candidates is not None or budget_exceeded:
            candidate_list = candidates or []
            self.line(
                f"uid={uid}  status={_status_value(status)}  "
                f"({len(candidate_list)} candidates)",
            )
            if estimate:
                self.line(
                    "  estimate: "
                    f"items={estimate.get('item_count', 0)} "
                    f"pages={estimate.get('page_count', 0)} "
                    f"seconds={estimate.get('audio_seconds', 0):.1f} "
                    f"tokens={estimate.get('audio_tokens', 0)}",
                )
            if budget_exceeded:
                self.line(f"  budget exceeded: {', '.join(budget_exceeded)}")
            if candidate_list:
                self.line(f"  candidates: {', '.join(candidate_list)}")
        else:
            self.line(f"uid={uid}  status={_status_value(status)}")
        self.asr_coverage(coverage)

    def asr_summary(
        self,
        summary: RunSummary,
        *,
        fallback_status: Any = None,
        candidates: list[str] | None = None,
        estimate: Mapping[str, Any] | None = None,
        budget_exceeded: list[str] | None = None,
    ) -> None:
        status = summary.asr.status
        if status is None and summary.run is not None:
            status = summary.run.status
        if status is None:
            status = fallback_status

        candidate_count = (
            len(candidates)
            if candidates is not None
            else summary.asr.candidate_count
        )
        suffix = f"  ({candidate_count} candidates)" if candidate_count is not None else ""
        self.line(f"uid={summary.uid}  status={_status_value(status)}{suffix}")
        if estimate:
            self.line(
                "  estimate: "
                f"items={estimate.get('item_count', 0)} "
                f"pages={estimate.get('page_count', 0)} "
                f"seconds={estimate.get('audio_seconds', 0):.1f} "
                f"tokens={estimate.get('audio_tokens', 0)}",
            )
        if budget_exceeded:
            self.line(f"  budget exceeded: {', '.join(budget_exceeded)}")
        if candidates:
            self.line(f"  candidates: {', '.join(candidates)}")
        if summary.asr.coverage_applicable:
            self.line(
                "  coverage: "
                f"success={summary.asr.success}/"
                f"{summary.asr.expected} "
                f"missing={summary.asr.missing} "
                f"failed={summary.asr.failed}",
            )
            if summary.asr.missing_bvids:
                self.line(f"  missing: {', '.join(summary.asr.missing_bvids)}")
            if summary.asr.failed_bvids:
                self.line(f"  failed: {', '.join(summary.asr.failed_bvids)}")
        if summary.asr.status_counts:
            counts = _format_counts(summary.asr.status_counts)
            if counts:
                self.line(f"  transcription rows: {counts}")
        self.attention_events(summary)

    def asr_coverage(self, coverage: Mapping[str, Any] | None) -> None:
        if not coverage:
            return
        self.line(
            "  coverage: "
            f"success={coverage.get('success', 0)}/"
            f"{coverage.get('expected', 0)} "
            f"missing={coverage.get('missing', 0)} "
            f"failed={coverage.get('failed', 0)}",
        )
        missing = coverage.get("missing_bvids") or []
        failed = coverage.get("failed_bvids") or []
        if missing:
            self.line(f"  missing: {', '.join(missing)}")
        if failed:
            self.line(f"  failed: {', '.join(failed)}")

    def delete_missing(self, *, uid: int) -> None:
        self.line(f"uid={uid}: no data found")

    def delete_plan(self, *, uid: int, raw: Any, workdir: Any) -> None:
        self.line(
            f"About to delete all data for uid={uid}:"
            f"\n  {raw}"
            f"\n  {workdir}/  (audio caches)",
        )

    def delete_cancelled(self) -> None:
        self.line("Cancelled")

    def delete_stats(self, stats: Mapping[str, int]) -> None:
        parts = ", ".join(f"{key}={value}" for key, value in stats.items())
        self.line(f"  {parts}")

    def doctor_report(self, report: Any) -> None:
        """Render a DoctorReport: one ``  <name>: <STATUS> (<detail>)`` line per check."""
        for result in report.results:
            detail = f" ({result.detail})" if result.detail else ""
            self.line(f"  {result.name}: {result.status.value}{detail}")

    def attention_events(self, summary: RunSummary, *, limit: int = 5) -> None:
        events = summary.recent_attention_events[-limit:]
        if not events:
            return
        self.line("  recent attention:")
        for event in events:
            target = event.item_id or event.endpoint or event.pipeline
            target_text = f" {target}" if target else ""
            detail = _event_detail(event.data)
            detail_text = f" ({detail})" if detail else ""
            self.line(f"    {event.event}{target_text}{detail_text}")


def _status_value(status: Any) -> str:
    if status is None:
        return "UNKNOWN"
    return str(getattr(status, "value", status))


def _format_counts(counts: Mapping[str, int]) -> str:
    return ", ".join(
        f"{status}={count}" for status, count in sorted(counts.items())
        if count
    )


def _is_failure_status(status: Any) -> bool:
    text = _status_value(status)
    return text == "PARTIAL" or text.startswith("FAILED") or text == "ERROR"


def _event_detail(data: Mapping[str, Any]) -> str:
    for key in ("error", "message"):
        value = data.get(key)
        if value:
            return str(value)
    bits = []
    for key in ("retry", "delay_s", "pieces", "missing", "failed"):
        if key in data and data[key] is not None:
            bits.append(f"{key}={data[key]}")
    return " ".join(bits)


__all__ = ["CliRenderer"]
