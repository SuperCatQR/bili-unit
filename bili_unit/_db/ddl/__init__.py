# bili_unit._db.ddl — DDL file loader.
#
# DDL is shipped as plain ``.sql`` files (not Python string literals) so it
# stays editable / lintable with normal SQL tooling and survives schema bumps
# without churning Python source.

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent

# Whitelist of DDL files we know how to load. Keeps bumps deliberate — adding
# main_v2 means adding the constant here and updating connection.SUPPORTED_*.
_DDL_FILES: dict[str, str] = {
    "main_v1": "main_v1.sql",
    "raw_v1": "raw_v1.sql",
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
