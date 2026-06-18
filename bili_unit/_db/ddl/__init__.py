# bili_unit._db.ddl — DDL file loader.
#
# DDL is shipped as plain ``.sql`` files (not Python string literals) so it
# stays editable / lintable with normal SQL tooling and survives schema bumps
# without churning Python source.

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent

# Whitelist of DDL files we know how to load. Keeps bumps deliberate: the
# current main schema is v4, while raw DB is v2.
_DDL_FILES: dict[str, str] = {
    "main_v4": "main_v4.sql",
    "raw_v2": "raw_v2.sql",
}


def read_ddl(name: str) -> str:
    """Return the verbatim text of a registered DDL file."""
    try:
        filename = _DDL_FILES[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown DDL '{name}'. Known: {sorted(_DDL_FILES)}",
        ) from exc
    return (_HERE / filename).read_text(encoding="utf-8")


__all__ = ["read_ddl"]
