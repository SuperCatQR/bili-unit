# bili_unit.processing._store -- SQLite-backed write store for the processing (asr) stage.
#
# Storage layout (single raw DB, {uid}.raw.db):
#
#   * audio_transcription  -- per-bvid pipeline output (ASR / subtitle / official).
#   * audio_transcription_page / audio_transcription_segment
#       Derived per-page / per-segment rows materialised from
#       ``audio_transcription.payload['result']['pages']`` on every successful
#       upsert (and cleared on non-success writes).
#   * stage_task[stage='asr']
#       Top-level task envelope. ``payload`` JSON carries the pipeline rollup:
#           {"pipelines": {"audio": {"status": "PENDING", "items": {...}}}}
#   * stage_error[stage='asr']
#       Per-item error sink (auto-increment id). Carries pipeline / item_type /
#       item_id columns; consumers who only care about audio can filter on
#       ``pipeline='audio'``.
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

_PROCESSING_STAGE = "asr"


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


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        """Upsert one ``audio_transcription`` row and rebuild derived rows.

        ``status`` must satisfy the table CHECK
        (``'pending'`` / ``'running'`` / ``'success'`` / ``'failed'`` /
        ``'skipped'``). ``payload`` is the ``ProcessingItemDTO``-shaped dict
        and is stored verbatim in the ``payload`` JSON column.  On success,
        ``audio_transcription_page`` and ``audio_transcription_segment`` are
        derived from ``payload.result.pages``; non-success writes clear stale
        derived rows.
        ``processed_at_ms`` defaults to the current wall clock when omitted.
        """
        ts = _now_ms() if processed_at_ms is None else processed_at_ms
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                """
                INSERT INTO audio_transcription
                    (bvid, status, transcription_source, transcript,
                     audio_tokens, seconds, cache_hits, payload, processed_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    status = excluded.status,
                    transcription_source = excluded.transcription_source,
                    transcript = excluded.transcript,
                    audio_tokens = excluded.audio_tokens,
                    seconds = excluded.seconds,
                    cache_hits = excluded.cache_hits,
                    payload = excluded.payload,
                    processed_at_ms = excluded.processed_at_ms
                """,
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
            ),
            (
                "DELETE FROM audio_transcription_page WHERE bvid = ?",
                (bvid,),
            ),
        ]

        result = payload.get("result") if isinstance(payload, dict) else None
        pages = result.get("pages") if isinstance(result, dict) else None
        if status == "success" and isinstance(pages, list):
            for fallback_page_no, page in enumerate(pages, start=1):
                if not isinstance(page, dict):
                    continue
                page_index = _coerce_int_or_none(page.get("page_index"))
                if page_index is None:
                    page_index = fallback_page_no - 1
                page_no = page_index + 1
                text = str(page.get("text") or "")
                segments = page.get("segments") or []
                if not isinstance(segments, list):
                    segments = []
                statements.append(
                    (
                        """
                        INSERT INTO audio_transcription_page
                            (bvid, page_no, page_index, cid, duration_s,
                             language, asr_model, transcript_text,
                             transcript_char_count, segment_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            bvid,
                            page_no,
                            page_index,
                            page.get("cid"),
                            _coerce_float_or_none(page.get("duration")),
                            page.get("language"),
                            page.get("asr_model"),
                            text,
                            len(text),
                            len([s for s in segments if isinstance(s, dict)]),
                        ),
                    ),
                )
                for segment_no, segment in enumerate(
                    (s for s in segments if isinstance(s, dict)),
                    start=1,
                ):
                    segment_text = str(segment.get("text") or "")
                    statements.append(
                        (
                            """
                            INSERT INTO audio_transcription_segment
                                (bvid, page_no, segment_no, start_seconds,
                                 end_seconds, duration_s, transcript_text,
                                 language, asr_model,
                                 is_empty_transcript_skip,
                                 is_high_risk_audio_skip, error_message)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                bvid,
                                page_no,
                                segment_no,
                                _coerce_float_or_none(segment.get("start_s")),
                                _coerce_float_or_none(segment.get("end_s")),
                                _coerce_float_or_none(segment.get("duration")),
                                segment_text,
                                segment.get("language") or page.get("language"),
                                segment.get("model") or page.get("asr_model"),
                                1 if segment.get("empty_skip") else 0,
                                1 if segment.get("high_risk_skip") else 0,
                                segment.get("error"),
                            ),
                        ),
                    )
        await self._ctx.conn.run_transaction(statements)

    # ------------------------------------------------------------------
    # audio transcription reads
    # ------------------------------------------------------------------

    async def get_audio_status(self, bvid: str) -> str | None:
        """Return the stored status for ``bvid`` or ``None`` if no row exists."""
        row = await self._ctx.conn.fetch_one(
            "SELECT status FROM audio_transcription WHERE bvid = ?",
            (bvid,),
        )
        if row is None:
            return None
        return row["status"]

    async def get_audio_payload(self, bvid: str) -> dict | None:
        """Return the stored payload dict for ``bvid`` or ``None`` if no row exists."""
        row = await self._ctx.conn.fetch_one(
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
            rows = await self._ctx.conn.fetch_all(
                "SELECT bvid FROM audio_transcription ORDER BY bvid",
            )
        else:
            rows = await self._ctx.conn.fetch_all(
                "SELECT bvid FROM audio_transcription WHERE status = ? ORDER BY bvid",
                (status,),
            )
        return [r["bvid"] for r in rows]

    async def list_failed_audio_bvids(self) -> list[str]:
        """Return bvids whose audio transcription is in the ``'failed'`` state."""
        return await self.list_audio_bvids(status="failed")

    async def list_audio_statuses(self) -> dict[str, str]:
        """Return current audio transcription status keyed by bvid."""
        rows = await self._ctx.conn.fetch_all(
            "SELECT bvid, status FROM audio_transcription ORDER BY bvid",
        )
        return {str(r["bvid"]): str(r["status"]) for r in rows}

    # ------------------------------------------------------------------
    # task state (stage_task[stage='asr'])
    # ------------------------------------------------------------------

    async def init_task(self, pipelines: list[str]) -> None:
        """Seed ``stage_task[stage='asr']`` with PENDING pipeline entries.

        Idempotent: re-running with the same (or overlapping) pipeline list
        uses ``INSERT OR IGNORE`` so any existing pipeline status / items are
        preserved. New pipelines that did not appear in a previous call are
        merged into the payload's ``pipelines`` dict.
        """
        now = _now_ms()
        async with self._task_lock:
            row = await self._ctx.conn.fetch_one(
                "SELECT payload FROM stage_task WHERE stage = ?",
                (_PROCESSING_STAGE,),
            )
            if row is None:
                payload = {
                    "pipelines": {
                        p: {"status": "PENDING", "items": {}} for p in pipelines
                    },
                }
                await self._ctx.conn.execute(
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
            await self._ctx.conn.execute(
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
        coverage: dict | None = None,
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
            row = await self._ctx.conn.fetch_one(
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
            if coverage is not None:
                entry["coverage"] = coverage
            await self._ctx.conn.execute(
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
        now = _now_ms()
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "UPDATE stage_task SET status = ?, updated_at_ms = ? WHERE stage = ?",
                (status, now, _PROCESSING_STAGE),
            ),
        ]
        if status not in {"PENDING", "RUNNING"}:
            statements.append(
                (
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("last_processed_at_ms", str(now)),
                ),
            )
        await self._ctx.conn.run_transaction(statements)

    async def get_task(self) -> dict | None:
        """Return ``{status, payload, created_at_ms, updated_at_ms}`` or ``None``.

        ``payload`` is decoded back to a dict; an empty / missing payload yields
        an empty dict in the returned mapping.
        """
        row = await self._ctx.conn.fetch_one(
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
    # error sink (stage_error[stage='asr'])
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
        row = await self._ctx.conn.fetch_one(
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
        rows = await self._ctx.conn.fetch_all(sql, tuple(params))
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
