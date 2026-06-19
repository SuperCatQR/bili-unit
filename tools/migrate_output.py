"""One-shot migration of legacy ``output/bili/`` artefacts to the current schema.

Background
----------
The unit's persistence layout used to be two SQLite files per uid:

    output/bili/{uid}.db       ← "main" DB written by the parsing stage
    output/bili/{uid}.raw.db   ← raw API payloads + fetch progress

Commit ``48874ad`` (2026-06) collapsed the parsing stage and merged everything
into a single file:

    output/bili/{uid}.raw.db   ← raw payloads + fetch progress + ASR results
                                 + stage_* runs/events/errors + manifest view
                                 (schema_version = 3)

A workspace that fetched/transcribed before that refactor still has files at
the old layout. The current code refuses to open them (raw.db is on
schema_version=2; the old {uid}.db has parsing-only tables that are now
unused). This tool migrates them in place:

  * Validate every {uid}.raw.db is at schema_version=2 and the legacy
    {uid}.db (if present) is at schema_version=4 with the expected ASR tables.
  * Snapshot the raw.db file to ``{uid}.raw.db.bak`` (one per uid).
  * Apply the v3 DDL on raw.db so the new tables exist (CREATE IF NOT EXISTS,
    so existing rows are untouched).
  * If a legacy main DB exists, ATTACH it and copy ASR + stage_* +
    fetch_endpoint_state into the raw.db. ``stage='parsing'`` rows are
    dropped because the new schema's CHECK constraint no longer allows them.
  * Bump meta.schema_version from 2 to 3 (and copy
    ``last_fetched_at_ms`` / ``last_processed_at_ms`` from the legacy main).
  * Delete the legacy ``{uid}.db`` (the .bak of raw.db is the rollback path).
  * Optionally clean orphan ASR temp dirs under
    ``output/bili/asr/temp/{uid}/audio/{bvid}/`` (no associated cache file).

The script is idempotent: re-running on an already-migrated workspace is a
no-op (raw.db is on v3, no legacy {uid}.db sitting next to it).

Usage
-----
::

    uv run python tools/migrate_output.py            # dry-run, default
    uv run python tools/migrate_output.py --apply    # do the migration
    uv run python tools/migrate_output.py --apply --clean-temp
    uv run python tools/migrate_output.py --apply --keep-legacy
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow running as ``python tools/migrate_output.py`` without an explicit
# ``-m``: prepend the repo root so ``bili_unit`` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bili_unit._db.connection import SUPPORTED_SCHEMA_VERSION  # noqa: E402
from bili_unit._db.ddl import read_ddl  # noqa: E402

# ASR cache schema version is owned by audio/_asr_cache. We just inspect the
# files to decide whether anything needs cleaning; no migration there.
LEGACY_RAW_SCHEMA_VERSION = 2
LEGACY_MAIN_SCHEMA_VERSION = 4

# Tables whose rows we copy from {uid}.db into {uid}.raw.db. Order matters:
# parents before children so foreign keys remain satisfiable mid-transaction.
_ASR_TABLES = (
    "audio_transcription",
    "audio_transcription_page",
    "audio_transcription_segment",
)
_STAGE_TABLES_FILTER_PARSING = (
    "stage_task",   # CHECK on stage now excludes 'parsing'
    "stage_event",  # carries stage column, may include parsing rows
)
_STAGE_TABLES_PLAIN = (
    "stage_run",            # no stage CHECK; copy verbatim
    "stage_error",          # CHECK is fetching/asr; v4 had no parsing rows in fixture
    "fetch_endpoint_state",
)
_META_KEYS_TO_CARRY = ("last_fetched_at_ms", "last_processed_at_ms")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@dataclass
class UidPlan:
    """What this script intends to do for one uid."""

    uid: int
    raw_db: Path
    legacy_main_db: Path | None  # None when no {uid}.db sitting next to raw.db
    raw_schema_version: int | None
    legacy_schema_version: int | None
    needs_raw_upgrade: bool
    asr_row_counts: dict[str, int] = field(default_factory=dict)
    stage_row_counts: dict[str, int] = field(default_factory=dict)
    parsing_rows_dropped: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return (
            not self.needs_raw_upgrade
            and self.legacy_main_db is None
        )


def _read_meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    """Return meta[key] as int, or None if absent / not numeric."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql).fetchone()[0])


def plan_uid(uid: int, root: Path) -> UidPlan:
    """Inspect one uid's files and decide what to migrate."""
    raw_db = root / f"{uid}.raw.db"
    legacy_main = root / f"{uid}.db"
    legacy_main_path: Path | None = legacy_main if legacy_main.exists() else None

    plan = UidPlan(
        uid=uid,
        raw_db=raw_db,
        legacy_main_db=legacy_main_path,
        raw_schema_version=None,
        legacy_schema_version=None,
        needs_raw_upgrade=False,
    )

    if not raw_db.exists():
        plan.blockers.append(f"{raw_db.name} not found")
        return plan

    raw_conn = sqlite3.connect(raw_db, timeout=2)
    try:
        raw_conn.row_factory = sqlite3.Row
        raw_v = _read_meta_int(raw_conn, "schema_version")
        plan.raw_schema_version = raw_v
        if raw_v == SUPPORTED_SCHEMA_VERSION:
            plan.needs_raw_upgrade = False
        elif raw_v == LEGACY_RAW_SCHEMA_VERSION:
            plan.needs_raw_upgrade = True
        else:
            plan.blockers.append(
                f"raw.db schema_version={raw_v} (expected "
                f"{LEGACY_RAW_SCHEMA_VERSION} or {SUPPORTED_SCHEMA_VERSION})",
            )
    finally:
        raw_conn.close()

    if legacy_main_path is None:
        return plan

    main_conn = sqlite3.connect(legacy_main_path, timeout=2)
    try:
        main_conn.row_factory = sqlite3.Row
        main_v = _read_meta_int(main_conn, "schema_version")
        plan.legacy_schema_version = main_v
        if main_v != LEGACY_MAIN_SCHEMA_VERSION:
            plan.blockers.append(
                f"legacy {legacy_main_path.name} schema_version="
                f"{main_v} (expected {LEGACY_MAIN_SCHEMA_VERSION}); "
                "this tool only knows v4",
            )
            return plan

        for t in _ASR_TABLES:
            if _table_exists(main_conn, t):
                plan.asr_row_counts[t] = _count(main_conn, t)
        for t in _STAGE_TABLES_PLAIN:
            if _table_exists(main_conn, t):
                plan.stage_row_counts[t] = _count(main_conn, t)
        for t in _STAGE_TABLES_FILTER_PARSING:
            if _table_exists(main_conn, t):
                total = _count(main_conn, t)
                parsing = _count(main_conn, t, "stage = 'parsing'")
                plan.stage_row_counts[t] = total - parsing
                if parsing:
                    plan.parsing_rows_dropped[t] = parsing
    finally:
        main_conn.close()

    return plan


def collect_plans(root: Path) -> list[UidPlan]:
    """Find every uid that has at least one DB file under ``root``."""
    if not root.is_dir():
        return []
    uids: set[int] = set()
    for p in root.iterdir():
        # Match {uid}.raw.db and legacy {uid}.db (but not asr/, etc.).
        if p.suffix != ".db":
            continue
        stem = (
            p.name[: -len(".raw.db")]
            if p.name.endswith(".raw.db")
            else p.name[: -len(".db")]
        )
        try:
            uids.add(int(stem))
        except ValueError:
            continue  # ignore non-uid filenames
    return [plan_uid(uid, root) for uid in sorted(uids)]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _backup_raw_db(plan: UidPlan, *, force: bool) -> Path:
    """Copy {uid}.raw.db to {uid}.raw.db.bak. Refuse to overwrite unless --force."""
    bak = plan.raw_db.with_suffix(plan.raw_db.suffix + ".bak")
    if bak.exists() and not force:
        raise RuntimeError(
            f"{bak.name} already exists; pass --force to overwrite or remove it",
        )
    shutil.copy2(plan.raw_db, bak)
    return bak


def _apply_v3_ddl(conn: sqlite3.Connection) -> None:
    """Run raw_v3.sql DDL. CREATE TABLE/INDEX/VIEW IF NOT EXISTS so it's safe
    to apply on top of an existing v2 raw.db."""
    ddl = read_ddl(f"raw_v{SUPPORTED_SCHEMA_VERSION}")
    conn.executescript(ddl)


def _bump_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("schema_version", str(SUPPORTED_SCHEMA_VERSION)),
    )


def _copy_table(
    conn: sqlite3.Connection,
    *,
    src_db: str,
    table: str,
    where: str = "",
) -> int:
    """INSERT OR IGNORE every row from src_db.table into main.table.

    Uses INSERT OR IGNORE so a re-run on partially-migrated state can resume
    without unique-constraint failures; rows the main already has stay as
    they are. Returns number of rows actually inserted.
    """
    before = int(conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
    sql = f"INSERT OR IGNORE INTO main.{table} SELECT * FROM {src_db}.{table}"
    if where:
        sql += f" WHERE {where}"
    conn.execute(sql)
    after = int(conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
    return after - before


def _carry_meta(conn: sqlite3.Connection, src_db: str) -> list[str]:
    """Copy whitelisted meta keys from legacy main → raw. Returns keys carried."""
    carried: list[str] = []
    for key in _META_KEYS_TO_CARRY:
        row = conn.execute(
            f"SELECT value FROM {src_db}.meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO main.meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, row[0]),
        )
        carried.append(key)
    return carried


def apply_plan(plan: UidPlan, *, force: bool, keep_legacy: bool) -> dict[str, object]:
    """Run the migration for one uid. Returns a small report dict."""
    if plan.blockers:
        raise RuntimeError(
            f"uid={plan.uid}: refusing to apply due to blockers: "
            + "; ".join(plan.blockers),
        )

    report: dict[str, object] = {
        "uid": plan.uid,
        "backup": None,
        "ddl_applied": False,
        "schema_bumped": False,
        "rows_copied": {},
        "rows_dropped": dict(plan.parsing_rows_dropped),
        "meta_carried": [],
        "legacy_db_removed": False,
    }

    if plan.is_noop:
        report["noop"] = True
        return report

    bak = _backup_raw_db(plan, force=force)
    report["backup"] = str(bak)

    conn = sqlite3.connect(plan.raw_db, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if plan.needs_raw_upgrade:
            _apply_v3_ddl(conn)
            report["ddl_applied"] = True

        if plan.legacy_main_db is not None:
            conn.execute(
                "ATTACH DATABASE ? AS legacy",
                (str(plan.legacy_main_db),),
            )
            try:
                conn.execute("BEGIN")
                copied: dict[str, int] = {}
                for t in _ASR_TABLES:
                    if t in plan.asr_row_counts:
                        copied[t] = _copy_table(conn, src_db="legacy", table=t)
                for t in _STAGE_TABLES_PLAIN:
                    if t in plan.stage_row_counts:
                        copied[t] = _copy_table(conn, src_db="legacy", table=t)
                for t in _STAGE_TABLES_FILTER_PARSING:
                    if t in plan.stage_row_counts:
                        copied[t] = _copy_table(
                            conn,
                            src_db="legacy",
                            table=t,
                            where="stage <> 'parsing'",
                        )
                report["meta_carried"] = _carry_meta(conn, "legacy")
                conn.execute("COMMIT")
                report["rows_copied"] = copied
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("DETACH DATABASE legacy")

        if plan.needs_raw_upgrade:
            _bump_schema_version(conn)
            report["schema_bumped"] = True
    finally:
        conn.close()

    if plan.legacy_main_db is not None and not keep_legacy:
        plan.legacy_main_db.unlink()
        # SQLite WAL/SHM siblings (rarely present after a clean close, but
        # the legacy main DB may still have them lingering).
        for suffix in ("-wal", "-shm", "-journal"):
            sibling = plan.legacy_main_db.with_name(plan.legacy_main_db.name + suffix)
            if sibling.exists():
                sibling.unlink()
        report["legacy_db_removed"] = True

    return report


# ---------------------------------------------------------------------------
# ASR temp cleanup
# ---------------------------------------------------------------------------


def find_stale_temp_dirs(root: Path) -> list[Path]:
    """List ``output/bili/asr/temp/{uid}/audio/{bvid}/`` dirs that can be deleted.

    A dir is considered stale if either:

    * It has at least one ``mp3_*`` subdir without a matching ``segments/``
      sibling (the ffmpeg pipeline did not finish — true "orphan"; the
      runtime cleanup helper looks for this same pattern), or
    * The bvid already has a cache file at
      ``output/bili/asr/cache/{uid}/{bvid}/{page_index}.json`` — the work
      is done and the temp dir is just a leftover of mp3 slices and the
      original m4s download.

    Returns the list of bvid-level dirs to remove.
    """
    base = root / "asr" / "temp"
    cache_root = root / "asr" / "cache"
    if not base.is_dir():
        return []
    stale: list[Path] = []
    for uid_dir in base.iterdir():
        if not uid_dir.is_dir():
            continue
        audio_dir = uid_dir / "audio"
        if not audio_dir.is_dir():
            continue
        cache_uid_dir = cache_root / uid_dir.name
        for bvid_dir in audio_dir.iterdir():
            if not bvid_dir.is_dir():
                continue

            # Done: any cache file exists for this bvid → temp is leftover.
            cache_bvid_dir = cache_uid_dir / bvid_dir.name
            if cache_bvid_dir.is_dir() and any(cache_bvid_dir.glob("*.json")):
                stale.append(bvid_dir)
                continue

            # Orphan: mp3_*/ without segments/ (or segments/ empty).
            for slice_dir in bvid_dir.glob("mp3_*"):
                if not slice_dir.is_dir():
                    continue
                segments_dir = slice_dir / "segments"
                if segments_dir.is_dir() and any(segments_dir.iterdir()):
                    continue  # active or completed slice, don't touch
                stale.append(bvid_dir)
                break
    return stale


def cleanup_stale_temp_dirs(root: Path, *, apply: bool) -> list[Path]:
    stale = find_stale_temp_dirs(root)
    if apply:
        for path in stale:
            shutil.rmtree(path, ignore_errors=True)
    return stale


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_plan(plan: UidPlan) -> str:
    if plan.blockers:
        return f"uid={plan.uid}: BLOCKED — " + "; ".join(plan.blockers)
    if plan.is_noop:
        return f"uid={plan.uid}: already on schema v{SUPPORTED_SCHEMA_VERSION}, no legacy main DB"
    parts: list[str] = [f"uid={plan.uid}:"]
    if plan.needs_raw_upgrade:
        parts.append(f"raw.db v{plan.raw_schema_version}->v{SUPPORTED_SCHEMA_VERSION}")
    if plan.legacy_main_db is not None:
        parts.append(f"copy from {plan.legacy_main_db.name}")
        for t, n in plan.asr_row_counts.items():
            parts.append(f"  {t}: {n} rows")
        for t, n in plan.stage_row_counts.items():
            extra = ""
            if t in plan.parsing_rows_dropped:
                extra = f" (drops {plan.parsing_rows_dropped[t]} parsing rows)"
            parts.append(f"  {t}: {n} rows{extra}")
    return "\n  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate output/bili/{uid}.{db,raw.db} to current schema.",
    )
    parser.add_argument(
        "--root",
        default="output/bili",
        type=Path,
        help="DB root (default: output/bili)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually perform the migration (default: dry-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing {uid}.raw.db.bak from a previous run",
    )
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="don't remove {uid}.db after successful migration",
    )
    parser.add_argument(
        "--clean-temp",
        action="store_true",
        help="also remove stale asr/temp dirs (orphans + bvids whose cache is complete)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    plans = collect_plans(root)
    print(f"# scanned {root}: {len(plans)} uid(s)")
    if not plans:
        print("nothing to do")
        return 0

    blockers = [p for p in plans if p.blockers]
    actionable = [p for p in plans if not p.blockers and not p.is_noop]

    print()
    print("=" * 60)
    print("Migration plan")
    print("=" * 60)
    for plan in plans:
        print()
        print(_format_plan(plan))

    stale_temp = find_stale_temp_dirs(root)
    if stale_temp:
        print()
        print(f"# asr/temp stale dirs: {len(stale_temp)}")
        for p in stale_temp[:10]:
            print(f"  {p}")
        if len(stale_temp) > 10:
            print(f"  ... +{len(stale_temp) - 10} more")

    if blockers:
        print()
        print(f"# {len(blockers)} uid(s) BLOCKED — fix these before --apply")
        return 2

    if not args.apply:
        print()
        print("# dry-run — pass --apply to perform the migration")
        if args.clean_temp and stale_temp:
            print(f"# would also remove {len(stale_temp)} stale temp dirs")
        return 0

    print()
    print("=" * 60)
    print("Applying...")
    print("=" * 60)
    for plan in actionable:
        report = apply_plan(plan, force=args.force, keep_legacy=args.keep_legacy)
        print()
        print(f"uid={report['uid']}: ok")
        if report.get("backup"):
            print(f"  backup: {report['backup']}")
        if report.get("ddl_applied"):
            print("  applied v3 DDL")
        if report.get("schema_bumped"):
            print(f"  schema_version → {SUPPORTED_SCHEMA_VERSION}")
        rows_copied = report.get("rows_copied") or {}
        for t, n in rows_copied.items():
            print(f"  copied {n} rows into {t}")
        rows_dropped = report.get("rows_dropped") or {}
        for t, n in rows_dropped.items():
            print(f"  dropped {n} parsing rows from {t}")
        meta_carried = report.get("meta_carried") or []
        if meta_carried:
            print(f"  carried meta: {', '.join(meta_carried)}")
        if report.get("legacy_db_removed"):
            print("  removed legacy {uid}.db")

    if args.clean_temp:
        removed = cleanup_stale_temp_dirs(root, apply=True)
        print()
        print(f"# removed {len(removed)} stale asr/temp dirs")

    print()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
