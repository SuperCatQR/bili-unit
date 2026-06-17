# bili_unit.processing._store -- SQLite-backed write store for the processing stage.
#
# Replaces the file-directory ``ProcessingDataStore`` + ``ProcessingErrorStore``
# pair (``bili_unit/processing/data.py`` + ``bili_unit/processing/error.py``).
# Phase 3 will rewire ``ProcessingRunner`` / ``ProcessingCommand`` to call this
# store; until then the two old stores stay in place.
#
# Storage layout (main DB only; processing does not touch the raw DB):
#
#   * audio_transcription  -- per-bvid pipeline output (ASR / subtitle / official).
#   * stage_task[stage='processing']
#       Top-level task envelope. ``payload`` JSON carries the pipeline rollup:
#           {"pipelines": {"audio": {"status": "PENDING", "items": {...}}}}
#   * stage_error[stage='processing']
#       Per-item error sink (auto-increment id). Carries pipeline / item_type /
#       item_id columns; consumers who only care about audio can filter on
#       ``pipeline='audio'``.
#
# FK constraint:
#   ``audio_transcription.bvid`` references ``video.bvid`` ON DELETE CASCADE.
#   In the live pipeline this is fine -- processing runs after parsing has
#   inserted the matching ``video`` row. Tests that exercise this store must
#   first insert a placeholder video row (or bypass FKs explicitly) before
#   calling :meth:`save_audio_transcription`.
#
# Concurrency:
#   Single-statement writes serialise through the ``Connection``'s asyncio.Lock.
#   :meth:`update_task_pipeline` is a read-modify-write; we add a store-local
#   lock so two concurrent callers can't interleave their reads/writes.

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .._db import UidContext

_PROCESSING_STAGE = "processing"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dump_json(value: dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: str | None) -> dict | None:
    if value is None or value == "":
        return None
    return json.loads(value)


def _retryable_to_int(retryable: bool | None) -> int | None:
    if retryable is None:
        return None
    return 1 if retryable else 0


def _retryable_from_int(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


class ProcessingStore:
    """SQLite-backed write store for the processing stage.

    Persists audio transcription results, pipeline task state, and per-item
    errors. Currently scoped to the audio pipeline; future pipelines (e.g.
    video frame analysis) would add new tables, not new methods on this store.

    The store binds to a :class:`UidContext`; callers are responsible for
    opening / closing the context.
    """

    def __init__(self, ctx: UidContext) -> None:
        self._ctx = ctx
        # Serialises read-modify-write task updates against each other so two
        # concurrent ``update_task_pipeline`` calls cannot drop one another's
        # change. Single-statement writes don't need this lock -- the
        # connection's own lock already serialises them.
        self._task_lock = asyncio.Lock()

    @property
    def ctx(self) -> UidContext:
        return self._ctx

    # ------------------------------------------------------------------
    # audio transcription writes
    # ------------------------------------------------------------------

    async def save_audio_transcription(
        self,
        bvid: str,
        *,
        status: str,
        transcription_source: str | None,
        transcript: str | None,
        audio_tokens: int | None,
        seconds: float | None,
        cache_hits: int | None,
        payload: dict,
        processed_at_ms: int | None = None,
    ) -> None:
        """Upsert one ``audio_transcription`` row (INSERT OR REPLACE).

        ``status`` must satisfy the table CHECK
        (``'pending'`` / ``'running'`` / ``'success'`` / ``'failed'`` /
        ``'skipped'``). ``payload`` is the ``ProcessingItemDTO``-shaped dict
        and is stored verbatim in the ``payload`` JSON column.
        ``processed_at_ms`` defaults to the current wall clock when omitted.
        """
        ts = _now_ms() if processed_at_ms is None else processed_at_ms
        await self._ctx.main.execute(
            "INSERT OR REPLACE INTO audio_transcription("
            "    bvid, status, transcription_source, transcript, "
            "    audio_tokens, seconds, cache_hits, payload, processed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bvid,
                status,
                transcription_source,
                transcript,
                audio_tokens,
                seconds,
                cache_hits,
                json.dumps(payload, ensure_ascii=False),
                ts,
            ),
        )

    # ------------------------------------------------------------------
    # audio transcription reads
    # ------------------------------------------------------------------

    async def get_audio_status(self, bvid: str) -> str | None:
        """Return the stored status for ``bvid`` or ``None`` if no row exists."""
        row = await self._ctx.main.fetch_one(
            "SELECT status FROM audio_transcription WHERE bvid = ?",
            (bvid,),
        )
        if row is None:
            return None
        return row["status"]

    async def get_audio_payload(self, bvid: str) -> dict | None:
        """Return the stored payload dict for ``bvid`` or ``None`` if no row exists."""
        row = await self._ctx.main.fetch_one(
            "SELECT payload FROM audio_transcription WHERE bvid = ?",
            (bvid,),
        )
        if row is None:
            return None
        return _load_json(row["payload"])

    async def list_audio_bvids(
        self, status: str | None = None,
    ) -> list[str]:
        """Return all bvids in ``audio_transcription``, optionally filtered by status."""
        if status is None:
            rows = await self._ctx.main.fetch_all(
                "SELECT bvid FROM audio_transcription ORDER BY bvid",
            )
        else:
            rows = await self._ctx.main.fetch_all(
                "SELECT bvid FROM audio_transcription WHERE status = ? ORDER BY bvid",
                (status,),
            )
        return [r["bvid"] for r in rows]

    async def list_failed_audio_bvids(self) -> list[str]:
        """Return bvids whose audio transcription is in the ``'failed'`` state."""
        return await self.list_audio_bvids(status="failed")

    # ------------------------------------------------------------------
    # task state (stage_task[stage='processing'])
    # ------------------------------------------------------------------

    async def init_task(self, pipelines: list[str]) -> None:
        """Seed ``stage_task[stage='processing']`` with PENDING pipeline entries.

        Idempotent: re-running with the same (or overlapping) pipeline list
        uses ``INSERT OR IGNORE`` so any existing pipeline status / items are
        preserved. New pipelines that did not appear in a previous call are
        merged into the payload's ``pipelines`` dict.
        """
        now = _now_ms()
        async with self._task_lock:
            row = await self._ctx.main.fetch_one(
                "SELECT payload FROM stage_task WHERE stage = ?",
                (_PROCESSING_STAGE,),
            )
            if row is None:
                payload = {
                    "pipelines": {
                        p: {"status": "PENDING", "items": {}} for p in pipelines
                    },
                }
                await self._ctx.main.execute(
                    "INSERT INTO stage_task("
                    "    stage, status, payload, created_at_ms, updated_at_ms"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        _PROCESSING_STAGE,
                        "PENDING",
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                return

            payload = _load_json(row["payload"]) or {}
            pipelines_dict = payload.setdefault("pipelines", {})
            mutated = False
            for p in pipelines:
                if p not in pipelines_dict:
                    pipelines_dict[p] = {"status": "PENDING", "items": {}}
                    mutated = True
            if not mutated:
                return  # nothing new to write
            await self._ctx.main.execute(
                "UPDATE stage_task SET payload = ?, updated_at_ms = ? "
                "WHERE stage = ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    _PROCESSING_STAGE,
                ),
            )

    async def update_task_pipeline(
        self,
        pipeline: str,
        status: str,
        items: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Atomically replace one pipeline entry's status (and optionally items).

        Reads the current ``stage_task`` payload, mutates only
        ``payload['pipelines'][pipeline]``, then writes the dict back. Other
        pipelines are left untouched. If the task row doesn't exist yet the
        call is a no-op (caller should have run :meth:`init_task` first).
        ``items=None`` keeps the existing items dict; pass an empty dict to
        clear it.
        """
        async with self._task_lock:
            row = await self._ctx.main.fetch_one(
                "SELECT payload FROM stage_task WHERE stage = ?",
                (_PROCESSING_STAGE,),
            )
            if row is None:
                return
            payload = _load_json(row["payload"]) or {}
            pipelines_dict = payload.setdefault("pipelines", {})
            entry = pipelines_dict.get(pipeline)
            if entry is None:
                entry = {"status": "PENDING", "items": {}}
                pipelines_dict[pipeline] = entry
            entry["status"] = status
            if items is not None:
                entry["items"] = items
            await self._ctx.main.execute(
                "UPDATE stage_task SET payload = ?, updated_at_ms = ? "
                "WHERE stage = ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    _now_ms(),
                    _PROCESSING_STAGE,
                ),
            )

    async def update_task_status(self, status: str) -> None:
        """Update the top-level ``stage_task.status`` and timestamp."""
        await self._ctx.main.execute(
            "UPDATE stage_task SET status = ?, updated_at_ms = ? WHERE stage = ?",
            (status, _now_ms(), _PROCESSING_STAGE),
        )

    async def get_task(self) -> dict | None:
        """Return ``{status, payload, created_at_ms, updated_at_ms}`` or ``None``.

        ``payload`` is decoded back to a dict; an empty / missing payload yields
        an empty dict in the returned mapping.
        """
        row = await self._ctx.main.fetch_one(
            "SELECT status, payload, created_at_ms, updated_at_ms "
            "FROM stage_task WHERE stage = ?",
            (_PROCESSING_STAGE,),
        )
        if row is None:
            return None
        return {
            "status": row["status"],
            "payload": _load_json(row["payload"]) or {},
            "created_at_ms": row["created_at_ms"],
            "updated_at_ms": row["updated_at_ms"],
        }

    # ------------------------------------------------------------------
    # error sink (stage_error[stage='processing'])
    # ------------------------------------------------------------------

    async def record_error(
        self,
        *,
        pipeline: str | None,
        item_type: str | None,
        item_id: str | None,
        error_type: str,
        message: str,
        retryable: bool | None,
        detail: dict | None = None,
        occurred_at_ms: int | None = None,
    ) -> int:
        """Insert one ``stage_error`` row and return the auto-generated id.

        ``retryable`` is tri-state: ``True`` / ``False`` / ``None`` (unknown);
        stored as 1 / 0 / NULL respectively.
        """
        ts = _now_ms() if occurred_at_ms is None else occurred_at_ms
        row = await self._ctx.main.fetch_one(
            "INSERT INTO stage_error("
            "    stage, endpoint, pipeline, item_type, item_id, "
            "    error_type, message, retryable, detail, occurred_at_ms"
            ") VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (
                _PROCESSING_STAGE,
                pipeline,
                item_type,
                item_id,
                error_type,
                message,
                _retryable_to_int(retryable),
                _dump_json(detail),
                ts,
            ),
        )
        assert row is not None  # RETURNING always yields a row on insert
        return int(row["id"])

    async def list_errors(
        self,
        *,
        pipeline: str | None = None,
        item_type: str | None = None,
        item_id: str | None = None,
    ) -> list[dict]:
        """Return processing-stage error rows newest-first, with optional filters.

        Each returned dict mirrors the persisted row plus a decoded
        ``detail`` dict and a tri-state ``retryable`` (``True``/``False``/``None``).
        Returns an empty list when no rows match.
        """
        where = ["stage = ?"]
        params: list[Any] = [_PROCESSING_STAGE]
        if pipeline is not None:
            where.append("pipeline = ?")
            params.append(pipeline)
        if item_type is not None:
            where.append("item_type = ?")
            params.append(item_type)
        if item_id is not None:
            where.append("item_id = ?")
            params.append(item_id)
        sql = (
            "SELECT id, stage, pipeline, item_type, item_id, "
            "       error_type, message, retryable, detail, occurred_at_ms "
            "FROM stage_error WHERE " + " AND ".join(where) + " "
            "ORDER BY id DESC"
        )
        rows = await self._ctx.main.fetch_all(sql, tuple(params))
        out: list[dict] = []
        for row in rows:
            out.append({
                "id": row["id"],
                "stage": row["stage"],
                "pipeline": row["pipeline"],
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "error_type": row["error_type"],
                "message": row["message"],
                "retryable": _retryable_from_int(row["retryable"]),
                "detail": _load_json(row["detail"]),
                "occurred_at_ms": row["occurred_at_ms"],
            })
        return out


__all__ = ["ProcessingStore"]
