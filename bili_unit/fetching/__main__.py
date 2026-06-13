# python -m bili_unit.fetching — thin backward-compat wrapper.
#
# Since the unified CLI (python -m bili_unit) now covers delete-uid,
# video-full, login, list-uids, and query, this module simply maps the
# legacy flag-based interface to the unified sub-command interface.
# New scripts should use ``python -m bili_unit <sub>`` directly.

from __future__ import annotations

import sys


def main() -> None:
    """Translate legacy fetching-CLI flags into unified bili_unit sub-commands."""
    args = sys.argv[1:]

    if "--login" in args or "-l" in args:
        sys.argv = ["bili_unit", "login"]
    elif "--list-uids" in args:
        sys.argv = ["bili_unit", "list-uids"]
    elif "--delete-uid" in args:
        uid = _extract_uid(args)
        extra = ["-y"] if ("--yes" in args or "-y" in args) else []
        sys.argv = ["bili_unit", "delete-uid", uid, *extra]
    elif "--query" in args or "-q" in args:
        uid = _extract_uid(args)
        sys.argv = ["bili_unit", "query", uid]
    elif args and not args[0].startswith("-"):
        uid = args[0]
        rest = args[1:]
        sys.argv = ["bili_unit", "fetch", uid, *rest]
    else:
        sys.argv = ["bili_unit", "fetch", *args]

    from bili_unit.__main__ import main as _uni_main
    _uni_main()


def _extract_uid(args: list[str]) -> str:
    """Pull the positional uid from a legacy arg list."""
    for a in args:
        if not a.startswith("-"):
            return a
    return ""


if __name__ == "__main__":
    main()
