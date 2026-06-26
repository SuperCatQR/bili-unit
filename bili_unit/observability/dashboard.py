"""Read-side dashboard snapshots for CLI, future TUI, and diagnostics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._db.paths import list_uids
from .._db.paths import resolve as resolve_paths
from .summary import RunSummary, load_run_summary


@dataclass(frozen=True)
class ManifestSnapshot:
    uid: int
    schema_version: int | None = None
    last_fetched_at_ms: int | None = None
    last_processed_at_ms: int | None = None
    endpoint_count: int = 0
    raw_payload_count: int = 0
    video_count: int = 0
    transcribed_count: int = 0
    transcription_failed_count: int = 0
    total_audio_tokens: int = 0
    total_audio_seconds: float = 0.0
    total_cache_hits: int = 0
    fetching_error_count: int = 0
    asr_error_count: int = 0


@dataclass(frozen=True)
class RecommendedAction:
    kind: str
    label: str
    command: str
    item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class UidDashboardSnapshot:
    uid: int
    db: Path
    workdir: Path
    manifest: ManifestSnapshot | None
    run_summary: RunSummary | None
    recommended_actions: list[RecommendedAction] = field(default_factory=list)
    read_error: str | None = None

    @property
    def available(self) -> bool:
        return self.read_error is None

    @property
    def active_stages(self) -> tuple[str, ...]:
        """Stages whose current task state is RUNNING."""
        if self.run_summary is None:
            return ()
        return tuple(stage for stage, task in sorted(self.run_summary.stage_tasks.items()) if task.status == "RUNNING")

    @property
    def active(self) -> bool:
        """Whether any known stage is currently RUNNING."""
        return bool(self.active_stages)


@dataclass(frozen=True)
class DashboardSnapshot:
    root: Path
    uids: list[int]
    items: list[UidDashboardSnapshot] = field(default_factory=list)


async def load_dashboard_snapshot(
    *,
    root: str | Path,
    uid: int | None = None,
    recent_limit: int = 20,
) -> DashboardSnapshot:
    """Load read-only per-uid facts for a dashboard/TUI surface."""
    root_path = Path(root)
    uids = [uid] if uid is not None else list_uids(root_path)
    items = [
        await load_uid_dashboard_snapshot(
            uid=selected_uid,
            root=root_path,
            recent_limit=recent_limit,
        )
        for selected_uid in uids
    ]
    return DashboardSnapshot(root=root_path, uids=uids, items=items)


async def load_uid_dashboard_snapshot(
    *,
    uid: int,
    root: str | Path,
    recent_limit: int = 20,
) -> UidDashboardSnapshot:
    """Load one uid's manifest and latest run summary without mutating state."""
    root_path = Path(root)
    paths = resolve_paths(uid, root_path)
    if not paths.raw_db.exists():
        return UidDashboardSnapshot(
            uid=uid,
            db=paths.raw_db,
            workdir=paths.workdir,
            manifest=None,
            run_summary=None,
            read_error="DB does not exist",
        )

    try:
        manifest = await _load_manifest(paths.raw_db, uid=uid)
        run_summary = await load_run_summary(
            uid=uid,
            root=root_path,
            recent_limit=recent_limit,
        )
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade per uid
        return UidDashboardSnapshot(
            uid=uid,
            db=paths.raw_db,
            workdir=paths.workdir,
            manifest=None,
            run_summary=None,
            read_error=str(exc),
        )

    return UidDashboardSnapshot(
        uid=uid,
        db=paths.raw_db,
        workdir=paths.workdir,
        manifest=manifest,
        run_summary=run_summary,
        recommended_actions=_recommend_actions(uid, run_summary),
    )


async def _load_manifest(path: Path, *, uid: int) -> ManifestSnapshot:
    import asyncio

    return await asyncio.to_thread(_load_manifest_sync, path, uid)


def _load_manifest_sync(path: Path, uid: int) -> ManifestSnapshot:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM manifest_summary").fetchone()
        if row is None:
            return ManifestSnapshot(uid=uid)
        return ManifestSnapshot(
            uid=_int_or(uid, row["uid"]),
            schema_version=_optional_int(row["schema_version"]),
            last_fetched_at_ms=_optional_int(row["last_fetched_at_ms"]),
            last_processed_at_ms=_optional_int(row["last_processed_at_ms"]),
            endpoint_count=_int_or(0, row["endpoint_count"]),
            raw_payload_count=_int_or(0, row["raw_payload_count"]),
            video_count=_int_or(0, row["video_count"]),
            transcribed_count=_int_or(0, row["transcribed_count"]),
            transcription_failed_count=_int_or(
                0,
                row["transcription_failed_count"],
            ),
            total_audio_tokens=_int_or(0, row["total_audio_tokens"]),
            total_audio_seconds=float(row["total_audio_seconds"] or 0.0),
            total_cache_hits=_int_or(0, row["total_cache_hits"]),
            fetching_error_count=_int_or(0, row["fetching_error_count"]),
            asr_error_count=_int_or(0, row["asr_error_count"]),
        )
    finally:
        conn.close()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _int_or(default: int, value: Any) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _recommend_actions(uid: int, summary: RunSummary) -> list[RecommendedAction]:
    actions: list[RecommendedAction] = []
    if summary.asr.failed_bvids:
        actions.append(
            RecommendedAction(
                kind="asr_retry_failed",
                label="Retry failed ASR items",
                command=f"uv run bili-unit asr {uid} --retry-failed-only",
                item_ids=tuple(summary.asr.failed_bvids),
            ),
        )
    if summary.asr.missing_bvids:
        bvids = tuple(summary.asr.missing_bvids)
        actions.append(
            RecommendedAction(
                kind="asr_run_missing",
                label="Run missing ASR items",
                command=(f"uv run bili-unit asr {uid} --only-bvids {' '.join(bvids)}"),
                item_ids=bvids,
            ),
        )
    return actions


__all__ = [
    "DashboardSnapshot",
    "ManifestSnapshot",
    "RecommendedAction",
    "UidDashboardSnapshot",
    "load_dashboard_snapshot",
    "load_uid_dashboard_snapshot",
]
