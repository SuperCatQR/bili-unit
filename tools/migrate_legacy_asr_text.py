"""Migrate legacy main DBs by preserving ASR transcript rows only.

This is intentionally not a general schema migration. Legacy v1 DBs contain
content that can be re-fetched and re-parsed, while ASR text can be expensive
to recreate. The tool backs up old main/raw DB files, creates a fresh current
main DB, and restores only rows from ``audio_transcription`` that carry text.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bili_unit._db.connection import SUPPORTED_MAIN_SCHEMA_VERSION
from bili_unit._db.ddl import read_ddl
from bili_unit._db.paths import list_uids, resolve


@dataclass(frozen=True)
class LegacyAsrRow:
    bvid: str
    status: str
    transcription_source: str | None
    transcript: str
    audio_tokens: int | None
    seconds: float | None
    cache_hits: int | None
    payload: str
    processed_at_ms: int


@dataclass(frozen=True)
class MigrationResult:
    uid: int
    migrated: bool
    source_schema_version: int | None
    asr_rows: int
    backup_dir: Path | None
    main_db: Path


def migrate_uid(
    uid: int,
    *,
    root: str | Path = "output/bili",
    backup_label: str | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """Migrate one uid's old DB, preserving ASR transcript text only."""
    paths = resolve(uid, root)
    if not paths.main_db.exists():
        raise FileNotFoundError(paths.main_db)

    version = _read_schema_version(paths.main_db)
    rows = _read_legacy_asr_rows(paths.main_db)
    if version == SUPPORTED_MAIN_SCHEMA_VERSION:
        return MigrationResult(
            uid=uid,
            migrated=False,
            source_schema_version=version,
            asr_rows=len(rows),
            backup_dir=None,
            main_db=paths.main_db,
        )

    backup_dir = _backup_dir(paths.root, uid, version, backup_label)
    if dry_run:
        return MigrationResult(
            uid=uid,
            migrated=False,
            source_schema_version=version,
            asr_rows=len(rows),
            backup_dir=backup_dir,
            main_db=paths.main_db,
        )

    backup_dir.mkdir(parents=True, exist_ok=False)
    _move_db_family(paths.main_db, backup_dir)
    _move_db_family(paths.raw_db, backup_dir)
    _create_current_main(paths.main_db, uid=uid, rows=rows)

    return MigrationResult(
        uid=uid,
        migrated=True,
        source_schema_version=version,
        asr_rows=len(rows),
        backup_dir=backup_dir,
        main_db=paths.main_db,
    )


def migrate_all(
    *,
    root: str | Path = "output/bili",
    backup_label: str | None = None,
    dry_run: bool = False,
) -> list[MigrationResult]:
    """Migrate every uid with a main DB under ``root``."""
    return [
        migrate_uid(
            uid,
            root=root,
            backup_label=backup_label,
            dry_run=dry_run,
        )
        for uid in list_uids(root)
    ]


def _read_schema_version(path: Path) -> int | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
        return None if row is None else int(row[0])
    finally:
        conn.close()


def _read_legacy_asr_rows(path: Path) -> list[LegacyAsrRow]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "audio_transcription"):
            return []
        rows = conn.execute(
            "SELECT bvid, status, transcription_source, transcript, "
            "       audio_tokens, seconds, cache_hits, payload, processed_at_ms "
            "FROM audio_transcription "
            "WHERE transcript IS NOT NULL AND length(trim(transcript)) > 0 "
            "ORDER BY bvid",
        ).fetchall()
        return [_coerce_asr_row(row) for row in rows]
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _coerce_asr_row(row: sqlite3.Row) -> LegacyAsrRow:
    bvid = str(row["bvid"])
    status = str(row["status"] or "success").lower()
    if status not in {"success", "skipped"}:
        status = "success"
    payload = _valid_payload_or_fallback(row["payload"], bvid=bvid)
    return LegacyAsrRow(
        bvid=bvid,
        status=status,
        transcription_source=row["transcription_source"],
        transcript=str(row["transcript"]),
        audio_tokens=_optional_int(row["audio_tokens"]),
        seconds=_optional_float(row["seconds"]),
        cache_hits=_optional_int(row["cache_hits"]),
        payload=payload,
        processed_at_ms=_required_ms(row["processed_at_ms"]),
    )


def _valid_payload_or_fallback(value: Any, *, bvid: str) -> str:
    if isinstance(value, str) and value.strip():
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            pass
    return json.dumps(
        {
            "legacy_migrated": True,
            "item_id": bvid,
            "status": "SUCCESS",
        },
        ensure_ascii=False,
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _required_ms(value: Any) -> int:
    if value is None or value == "":
        return int(time.time() * 1000)
    return int(value)


def _backup_dir(
    root: Path,
    uid: int,
    version: int | None,
    backup_label: str | None,
) -> Path:
    label = backup_label or time.strftime("%Y%m%d-%H%M%S")
    version_label = "unknown" if version is None else f"v{version}"
    base = root / f"{uid}.legacy-{version_label}-{label}"
    candidate = base
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{base.name}-{suffix}"
    return candidate


def _move_db_family(db_path: Path, backup_dir: Path) -> None:
    for path in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ):
        if path.exists():
            shutil.move(str(path), str(backup_dir / path.name))


def _create_current_main(
    path: Path,
    *,
    uid: int,
    rows: list[LegacyAsrRow],
) -> None:
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(read_ddl(f"main_v{SUPPORTED_MAIN_SCHEMA_VERSION}"))
        meta_rows = [
            ("schema_version", str(SUPPORTED_MAIN_SCHEMA_VERSION)),
            ("uid", str(uid)),
            ("created_at_ms", str(now_ms)),
        ]
        if rows:
            meta_rows.append(("last_processed_at_ms", str(max(r.processed_at_ms for r in rows))))
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            meta_rows,
        )
        for row in rows:
            _insert_legacy_asr_row(conn, row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_legacy_asr_row(conn: sqlite3.Connection, row: LegacyAsrRow) -> None:
    video_payload = json.dumps(
        {
            "legacy_migrated": True,
            "bvid": row.bvid,
            "note": "Placeholder row kept so legacy ASR transcript can remain addressable.",
        },
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO video(bvid, title, payload, parsed_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (row.bvid, f"legacy ASR placeholder {row.bvid}", video_payload, row.processed_at_ms),
    )
    conn.execute(
        "INSERT INTO audio_transcription("
        "    bvid, status, transcription_source, transcript, audio_tokens, "
        "    seconds, cache_hits, payload, processed_at_ms"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.bvid,
            row.status,
            row.transcription_source,
            row.transcript,
            row.audio_tokens,
            row.seconds,
            row.cache_hits,
            row.payload,
            row.processed_at_ms,
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preserve ASR text from legacy bili-unit DBs and reset DBs to the current schema.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--uid", type=int, help="Migrate one uid")
    target.add_argument("--all", action="store_true", help="Migrate every uid under --root")
    parser.add_argument("--root", default="output/bili", help="DB root directory")
    parser.add_argument("--backup-label", default=None, help="Stable label for backup dirs")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    results = (
        migrate_all(root=args.root, backup_label=args.backup_label, dry_run=args.dry_run)
        if args.all
        else [migrate_uid(args.uid, root=args.root, backup_label=args.backup_label, dry_run=args.dry_run)]
    )
    for result in results:
        action = "migrated" if result.migrated else "skipped"
        print(
            f"uid={result.uid} {action} "
            f"schema={result.source_schema_version} "
            f"asr_rows={result.asr_rows} "
            f"backup={result.backup_dir}",
        )


if __name__ == "__main__":
    main()
