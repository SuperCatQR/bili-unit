# bili_unit._db.paths — uid → on-disk paths.
#
# One uid maps to three locations:
#
#   output/bili/{uid}.db        <- main DB  (consumer contract)
#   output/bili/{uid}.raw.db    <- raw DB   (producer-private)
#   output/bili/{uid}/          <- workdir  (audio caches / temp files)
#
# All three are derived from a single root (BiliSettings.bili_db_dir),
# replacing the old per-stage data_dir / error_dir / temp_dir layout.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Suffixes are public — `tools/migrate_jsonkv_to_sqlite.py` and consumers that
# enumerate the directory both rely on them. Don't change without a schema bump.
MAIN_DB_SUFFIX = ".db"
RAW_DB_SUFFIX = ".raw.db"


@dataclass(frozen=True, slots=True)
class UidPaths:
    """Resolved on-disk locations for a single uid."""

    uid: int
    root: Path
    main_db: Path
    raw_db: Path
    workdir: Path

    @property
    def images_dir(self) -> Path:
        return self.workdir / "images"

    @property
    def audio_dir(self) -> Path:
        return self.workdir / "audio"


def resolve(uid: int, root: str | Path) -> UidPaths:
    """Return the canonical paths for ``uid`` under ``root``.

    Does not create any of the directories — that's the connection layer's job.
    Pass ``BiliSettings.bili_db_dir`` (or ``output/bili`` by default) as ``root``.
    """
    if uid <= 0:
        raise ValueError(f"uid must be a positive integer, got {uid}")
    root_path = Path(root)
    return UidPaths(
        uid=uid,
        root=root_path,
        main_db=root_path / f"{uid}{MAIN_DB_SUFFIX}",
        raw_db=root_path / f"{uid}{RAW_DB_SUFFIX}",
        workdir=root_path / str(uid),
    )


def list_uids(root: str | Path) -> list[int]:
    """Return every uid that has a main DB under ``root``, sorted ascending.

    Replaces the old ``Query.list_tasks()`` for the CLI's deleted ``list-uids``
    subcommand — kept here as a tiny utility because callers (host apps,
    migration scripts) still need it occasionally.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    out: list[int] = []
    # Glob ``*.db`` then exclude raw — simpler than two globs with overlap.
    for p in root_path.glob(f"*{MAIN_DB_SUFFIX}"):
        if p.name.endswith(RAW_DB_SUFFIX):
            continue
        stem = p.name[: -len(MAIN_DB_SUFFIX)]
        try:
            out.append(int(stem))
        except ValueError:
            continue  # foreign file, skip
    out.sort()
    return out


__all__ = [
    "MAIN_DB_SUFFIX",
    "RAW_DB_SUFFIX",
    "UidPaths",
    "list_uids",
    "resolve",
]
