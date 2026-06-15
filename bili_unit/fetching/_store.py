# bili_unit.fetching._store — SQLite-backed write store for the fetching stage.
#
# Replaces the file-directory ``DataStore`` + ``ErrorStore`` pair in
# ``bili_unit/fetching/data.py`` and ``bili_unit/fetching/error.py``.
#
# Storage split:
#   * raw DB ({uid}.raw.db)
#       - raw_payload: every endpoint response, keyed by (endpoint, item_id).
#         item_id='' for endpoint-level / paginated endpoints; bvid/cvid/...
#         for fanout endpoints.
#       - fetch_progress: pagination cursor per endpoint (commit marker for
#         the atomic page+progress write).
#   * main DB ({uid}.db)
#       - stage_task[stage='fetching']: top-level task envelope (status +
#         endpoints list in payload JSON).
#       - fetch_endpoint_state: per-endpoint state machine row
#         (status / retry_count / last_error_id / item_progress / progress).
#       - stage_error[stage='fetching']: error sink (auto-increment id).
#
# Concurrency:
#   The underlying ``Connection`` serialises writes through an asyncio.Lock,
#   so the store needs no extra locking. Multi-statement atomic writes use
#   ``run_transaction`` which holds the lock for the whole BEGIN/COMMIT.
#
# Phase 3 will rewire ``Runner`` to call this store; the call sites are
# documented in the per-method docstrings to make that mechanical.

from __future__ import annotations

import json
import time
from typing import Any

from .._db import UidContext

_FETCHING_STAGE = "fetching"


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


class FetchingStore:
    """SQLite-backed write store for the fetching stage.

    Reads/writes the *raw* DB (raw_payload + fetch_progress tables) and the
    *main* DB (stage_task[stage='fetching'] + fetch_endpoint_state +
    stage_error[stage='fetching']). Held by ``FetchingCommand`` for the
    duration of a single ``fetch_uid`` run.
    """

    def __init__(self, ctx: UidContext) -> None:
        self._ctx = ctx

    @property
    def ctx(self) -> UidContext:
        return self._ctx

    # ------------------------------------------------------------------
    # raw DB writes (raw_payload + fetch_progress)
    # ------------------------------------------------------------------

    async def save_raw_payload(
        self,
        endpoint: str,
        item_id: str,
        payload: dict,
        *,
        fetched_at_ms: int | None = None,
    ) -> None:
        """Upsert a raw_payload row.

        ``item_id=''`` for endpoint-level / paginated responses; bvid / cvid /
        opus_id / dynamic_id / rlid for fanout-endpoint per-item responses.
        """
        ts = _now_ms() if fetched_at_ms is None else fetched_at_ms
        await self._ctx.raw.execute(
            "INSERT INTO raw_payload(endpoint, item_id, payload, fetched_at_ms) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint, item_id) DO UPDATE SET "
            "    payload = excluded.payload, "
            "    fetched_at_ms = excluded.fetched_at_ms",
            (endpoint, item_id, json.dumps(payload, ensure_ascii=False), ts),
        )

    async def save_raw_page_and_progress(
        self,
        endpoint: str,
        item_id: str,
        payload: dict,
        progress: dict,
        *,
        fetched_at_ms: int | None = None,
    ) -> None:
        """Atomically write raw_payload + fetch_progress.

        Payload is written first, progress last (commit marker). Both rows
        commit in a single transaction so a crash leaves the raw DB in a
        consistent state.
        """
        ts = _now_ms() if fetched_at_ms is None else fetched_at_ms
        cursor = progress.get("cursor")
        # cursor accepts JSON-encodable values (dict for next_request, str for
        # plain cursors) — we always store as TEXT.
        if cursor is not None and not isinstance(cursor, str):
            cursor_str: str | None = json.dumps(cursor, ensure_ascii=False)
        else:
            cursor_str = cursor
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "INSERT INTO raw_payload(endpoint, item_id, payload, fetched_at_ms) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(endpoint, item_id) DO UPDATE SET "
                "    payload = excluded.payload, "
                "    fetched_at_ms = excluded.fetched_at_ms",
                (endpoint, item_id, json.dumps(payload, ensure_ascii=False), ts),
            ),
            (
                "INSERT INTO fetch_progress(endpoint, cursor, total, fetched, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET "
                "    cursor = excluded.cursor, "
                "    total = excluded.total, "
                "    fetched = excluded.fetched, "
                "    updated_at_ms = excluded.updated_at_ms",
                (
                    endpoint,
                    cursor_str,
                    progress.get("total"),
                    progress.get("fetched"),
                    ts,
                ),
            ),
        ]
        await self._ctx.raw.run_transaction(statements)

    async def save_progress(
        self,
        endpoint: str,
        progress: dict,
        *,
        updated_at_ms: int | None = None,
    ) -> None:
        """Upsert a fetch_progress row (pagination cursor / counters)."""
        ts = _now_ms() if updated_at_ms is None else updated_at_ms
        cursor = progress.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            cursor_str: str | None = json.dumps(cursor, ensure_ascii=False)
        else:
            cursor_str = cursor
        await self._ctx.raw.execute(
            "INSERT INTO fetch_progress(endpoint, cursor, total, fetched, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET "
            "    cursor = excluded.cursor, "
            "    total = excluded.total, "
            "    fetched = excluded.fetched, "
            "    updated_at_ms = excluded.updated_at_ms",
            (endpoint, cursor_str, progress.get("total"), progress.get("fetched"), ts),
        )

    # ------------------------------------------------------------------
    # raw DB reads (incremental-mode helpers)
    # ------------------------------------------------------------------

    async def get_raw_payload(
        self, endpoint: str, item_id: str = "",
    ) -> dict | None:
        """Return the stored payload dict for (endpoint, item_id), or None."""
        row = await self._ctx.raw.fetch_one(
            "SELECT payload FROM raw_payload WHERE endpoint = ? AND item_id = ?",
            (endpoint, item_id),
        )
        if row is None:
            return None
        return json.loads(row["payload"])

    async def get_progress(self, endpoint: str) -> dict | None:
        """Return {cursor, total, fetched, updated_at_ms} for ``endpoint``, or None."""
        row = await self._ctx.raw.fetch_one(
            "SELECT cursor, total, fetched, updated_at_ms FROM fetch_progress "
            "WHERE endpoint = ?",
            (endpoint,),
        )
        if row is None:
            return None
        cursor_raw = row["cursor"]
        # Try to JSON-decode cursors that look like a serialised dict; otherwise
        # return the raw string. Plain string cursors round-trip unchanged.
        cursor: Any = cursor_raw
        if isinstance(cursor_raw, str) and cursor_raw and cursor_raw[0] in "{[":
            try:
                cursor = json.loads(cursor_raw)
            except json.JSONDecodeError:
                cursor = cursor_raw
        return {
            "cursor": cursor,
            "total": row["total"],
            "fetched": row["fetched"],
            "updated_at_ms": row["updated_at_ms"],
        }

    async def list_completed_items(self, endpoint: str) -> list[str]:
        """Return the item_ids that already have a raw_payload row.

        Excludes the endpoint-level row (item_id='') so callers get only the
        fanout entries. Replaces ``data.list_prefix("uid:N:fetch:ep:")`` which
        the old runner used to skip already-fetched items in incremental mode.
        """
        rows = await self._ctx.raw.fetch_all(
            "SELECT item_id FROM raw_payload "
            "WHERE endpoint = ? AND item_id <> '' "
            "ORDER BY item_id",
            (endpoint,),
        )
        return [r["item_id"] for r in rows]

    async def list_fanout_payloads(self, endpoint: str) -> dict[str, dict]:
        """Return ``{item_id: payload}`` for every fanout row of ``endpoint``."""
        rows = await self._ctx.raw.fetch_all(
            "SELECT item_id, payload FROM raw_payload "
            "WHERE endpoint = ? AND item_id <> ''",
            (endpoint,),
        )
        return {r["item_id"]: json.loads(r["payload"]) for r in rows}

    async def list_item_ages_ms(self, endpoint: str) -> dict[str, int]:
        """Return ``{item_id: fetched_at_ms}`` for refresh-mode age comparisons."""
        rows = await self._ctx.raw.fetch_all(
            "SELECT item_id, fetched_at_ms FROM raw_payload "
            "WHERE endpoint = ? AND item_id <> ''",
            (endpoint,),
        )
        return {r["item_id"]: r["fetched_at_ms"] for r in rows}

    # ------------------------------------------------------------------
    # main DB writes (task + endpoint state)
    # ------------------------------------------------------------------

    async def init_task(self, endpoints: list[str]) -> None:
        """Insert task envelope + per-endpoint state rows (idempotent).

        First call seeds ``stage_task[stage='fetching']`` with status=PENDING
        and the endpoint list embedded in the payload JSON, plus one
        ``fetch_endpoint_state`` row per endpoint (status=PENDING). Re-running
        with the same or overlapping endpoints uses INSERT OR IGNORE so an
        existing row's status / retry_count / item_progress are preserved.
        """
        now = _now_ms()
        payload_json = json.dumps({"endpoints": list(endpoints)}, ensure_ascii=False)
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "INSERT OR IGNORE INTO stage_task("
                "    stage, status, payload, created_at_ms, updated_at_ms"
                ") VALUES (?, ?, ?, ?, ?)",
                (_FETCHING_STAGE, "PENDING", payload_json, now, now),
            ),
        ]
        for ep in endpoints:
            statements.append(
                (
                    "INSERT OR IGNORE INTO fetch_endpoint_state("
                    "    endpoint, status, retry_count, last_error_id, "
                    "    item_progress, progress, updated_at_ms"
                    ") VALUES (?, ?, 0, NULL, NULL, NULL, ?)",
                    (ep, "PENDING", now),
                ),
            )
        await self._ctx.main.run_transaction(statements)

    async def update_task_status(self, status: str) -> None:
        """Update ``stage_task[stage='fetching'].status`` and timestamp."""
        await self._ctx.main.execute(
            "UPDATE stage_task SET status = ?, updated_at_ms = ? WHERE stage = ?",
            (status, _now_ms(), _FETCHING_STAGE),
        )

    async def update_endpoint_state(
        self,
        endpoint: str,
        *,
        status: str,
        retry_count: int = 0,
        last_error_id: int | None = None,
        item_progress: dict | None = None,
        progress: dict | None = None,
    ) -> None:
        """Upsert a fetch_endpoint_state row for ``endpoint``.

        Existing rows: ``status`` and ``retry_count`` always overwrite;
        ``last_error_id``, ``item_progress``, ``progress`` overwrite when
        provided, otherwise the previous value is preserved (COALESCE on
        NULL). Pass an explicit empty dict to clear ``item_progress`` /
        ``progress``.
        """
        now = _now_ms()
        item_progress_json = _dump_json(item_progress)
        progress_json = _dump_json(progress)
        await self._ctx.main.execute(
            "INSERT INTO fetch_endpoint_state("
            "    endpoint, status, retry_count, last_error_id, "
            "    item_progress, progress, updated_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET "
            "    status = excluded.status, "
            "    retry_count = excluded.retry_count, "
            "    last_error_id = COALESCE(excluded.last_error_id, fetch_endpoint_state.last_error_id), "
            "    item_progress = COALESCE(excluded.item_progress, fetch_endpoint_state.item_progress), "
            "    progress = COALESCE(excluded.progress, fetch_endpoint_state.progress), "
            "    updated_at_ms = excluded.updated_at_ms",
            (
                endpoint,
                status,
                retry_count,
                last_error_id,
                item_progress_json,
                progress_json,
                now,
            ),
        )

    # ------------------------------------------------------------------
    # main DB reads (state machine queries)
    # ------------------------------------------------------------------

    async def get_task_status(self) -> str | None:
        """Return ``stage_task[stage='fetching'].status`` or None."""
        row = await self._ctx.main.fetch_one(
            "SELECT status FROM stage_task WHERE stage = ?",
            (_FETCHING_STAGE,),
        )
        if row is None:
            return None
        return row["status"]

    async def get_endpoint_status(self, endpoint: str) -> str | None:
        """Return the endpoint's current status string, or None if no row."""
        row = await self._ctx.main.fetch_one(
            "SELECT status FROM fetch_endpoint_state WHERE endpoint = ?",
            (endpoint,),
        )
        if row is None:
            return None
        return row["status"]

    async def get_endpoint_state(self, endpoint: str) -> dict | None:
        """Return the full endpoint-state row as a dict, or None if missing.

        JSON columns (``item_progress``, ``progress``) are decoded into nested
        dicts; missing/empty are returned as None (matches the old DataStore
        convention).
        """
        row = await self._ctx.main.fetch_one(
            "SELECT endpoint, status, retry_count, last_error_id, "
            "       item_progress, progress, updated_at_ms "
            "FROM fetch_endpoint_state WHERE endpoint = ?",
            (endpoint,),
        )
        if row is None:
            return None
        return {
            "endpoint": row["endpoint"],
            "status": row["status"],
            "retry_count": row["retry_count"],
            "last_error_id": row["last_error_id"],
            "item_progress": _load_json(row["item_progress"]),
            "progress": _load_json(row["progress"]),
            "updated_at_ms": row["updated_at_ms"],
        }

    async def list_failed_items(self, endpoint: str) -> list[str]:
        """Return item_ids whose last attempt failed for ``endpoint``.

        Reads ``stage_error[stage='fetching']`` and pulls ``item_id`` out of
        ``detail`` JSON. Then drops any item_id that has since been written
        successfully to ``raw_payload`` (retry-to-success supersedes the
        historical error). The result is sorted, deduplicated, and ready to
        feed back into a retry-only fetch.
        """
        error_rows = await self._ctx.main.fetch_all(
            "SELECT detail FROM stage_error "
            "WHERE stage = ? AND endpoint = ? AND detail IS NOT NULL",
            (_FETCHING_STAGE, endpoint),
        )
        failed: set[str] = set()
        for row in error_rows:
            try:
                detail = json.loads(row["detail"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(detail, dict):
                continue
            item_id = detail.get("item_id")
            if item_id:
                failed.add(str(item_id))
        if not failed:
            return []
        succeeded = set(await self.list_completed_items(endpoint))
        return sorted(failed - succeeded)

    # ------------------------------------------------------------------
    # error sink (stage_error)
    # ------------------------------------------------------------------

    async def record_error(
        self,
        *,
        endpoint: str | None,
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
        retryable_int: int | None = (
            None if retryable is None else (1 if retryable else 0)
        )
        detail_json = _dump_json(detail)
        # SQLite 3.35+ supports INSERT ... RETURNING; we run it through
        # fetch_one to read back the new rowid in a single round-trip.
        row = await self._ctx.main.fetch_one(
            "INSERT INTO stage_error("
            "    stage, endpoint, pipeline, item_type, item_id, "
            "    error_type, message, retryable, detail, occurred_at_ms"
            ") VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (
                _FETCHING_STAGE,
                endpoint,
                error_type,
                message,
                retryable_int,
                detail_json,
                ts,
            ),
        )
        assert row is not None  # RETURNING always yields a row on insert
        return int(row["id"])

    async def list_errors(
        self, endpoint: str | None = None,
    ) -> list[dict]:
        """Return error rows (newest first), optionally filtered by endpoint.

        ``retryable`` is decoded back to ``True``/``False``/``None``;
        ``detail`` is decoded to a dict (or None when absent).
        """
        if endpoint is None:
            rows = await self._ctx.main.fetch_all(
                "SELECT id, stage, endpoint, error_type, message, retryable, "
                "       detail, occurred_at_ms "
                "FROM stage_error WHERE stage = ? "
                "ORDER BY id DESC",
                (_FETCHING_STAGE,),
            )
        else:
            rows = await self._ctx.main.fetch_all(
                "SELECT id, stage, endpoint, error_type, message, retryable, "
                "       detail, occurred_at_ms "
                "FROM stage_error WHERE stage = ? AND endpoint = ? "
                "ORDER BY id DESC",
                (_FETCHING_STAGE, endpoint),
            )
        out: list[dict] = []
        for row in rows:
            retryable: bool | None = (
                None if row["retryable"] is None else bool(row["retryable"])
            )
            out.append(
                {
                    "id": row["id"],
                    "stage": row["stage"],
                    "endpoint": row["endpoint"],
                    "error_type": row["error_type"],
                    "message": row["message"],
                    "retryable": retryable,
                    "detail": _load_json(row["detail"]),
                    "occurred_at_ms": row["occurred_at_ms"],
                }
            )
        return out


__all__ = ["FetchingStore"]
