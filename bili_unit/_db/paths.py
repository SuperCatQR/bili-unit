# bili_unit._db.paths — uid → on-disk paths.
#
# One uid maps to two locations:
#
#   output/bili/{uid}.raw.db    <- the unit's only DB file
#   output/bili/{uid}/          <- workdir (audio caches / temp files)
#
# Both are derived from a single root (BiliSettings.bili_db_dir, default
# ``output/bili``).
#
# History note: earlier schema versions also wrote a separate
# ``output/bili/{uid}.db`` "main" DB populated by a parsing stage that no
# longer exists. The unit now writes raw.db only; if you have a stale
# ``{uid}.db`` next to a current ``{uid}.raw.db`` it is a leftover from
# an older build and can be deleted.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Suffix is public — consumers that enumerate the directory rely on it.
# Don't change without a schema bump.
RAW_DB_SUFFIX = ".raw.db"


@dataclass(frozen=True, slots=True)
class UidPaths:
    """Resolved on-disk locations for a single uid."""

    uid: int
    root: Path
    raw_db: Path
    workdir: Path

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
        raw_db=root_path / f"{uid}{RAW_DB_SUFFIX}",
        workdir=root_path / str(uid),
    )


def list_uids(root: str | Path) -> list[int]:
    """Return every uid that has a raw DB under ``root``, sorted ascending."""
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    out: list[int] = []
    for p in root_path.glob(f"*{RAW_DB_SUFFIX}"):
        stem = p.name[: -len(RAW_DB_SUFFIX)]
        try:
            out.append(int(stem))
        except ValueError:
            continue  # foreign file, skip
    out.sort()
    return out


__all__ = [
    "RAW_DB_SUFFIX",
    "UidPaths",
    "list_uids",
    "resolve",
]
